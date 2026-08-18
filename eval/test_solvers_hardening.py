"""Unit tests for hardened search solvers (temporal delta, math aggregator)."""

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


def test_natural_date_parsing():
    res1 = parse_natural_or_iso_date("I attended the charity dinner on February 14th, 2023")
    assert res1 is not None
    dt1, str1 = res1
    assert dt1.year == 2023
    assert dt1.month == 2
    assert dt1.day == 14

    res2 = parse_natural_or_iso_date("Trip was on 15th of May 2023")
    assert res2 is not None
    dt2, str2 = res2
    assert dt2.month == 5
    assert dt2.day == 15


def test_relative_temporal_delta_with_as_of():
    as_of = datetime(2023, 5, 20, tzinfo=timezone.utc).timestamp()
    candidates = [
        ("mem1", "[Session Date: 2023-05-15]\nI bought a smoker at Home Depot.", "", "", "2023-05-15 10:00:00"),
    ]
    query = "How many days ago did I buy a smoker?"
    res = calculate_temporal_delta(query, candidates, as_of=as_of)
    assert res is not None
    assert "5 days" in res


def test_inter_event_temporal_delta():
    candidates = [
        ("mem1", "[Session Date: 2023-05-10]\nI visited the Museum of Modern Art (MoMA).", "", "", "2023-05-10 10:00:00"),
        ("mem2", "[Session Date: 2023-05-17]\nI visited the Metropolitan Museum of Art.", "", "", "2023-05-17 12:00:00"),
    ]
    query = "How many days passed between my visit to MoMA and the Metropolitan Museum of Art?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "7 days" in res


def test_price_difference_solver():
    candidates = [
        ("mem1", "I bought luxury boots for $400 at Nordstrom.", "", "", ""),
        ("mem2", "I saw a similar pair at the budget store for $120.", "", "", ""),
    ]
    query = "What is the difference in price between my luxury boots and the similar pair found at the budget store?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$280" in res


def test_total_cost_math_aggregator():
    candidates = [
        ("mem1", "I bought tennis racket for $150.", "", "", ""),
        ("mem2", "I spent $50 on shoes and $25 on balls.", "", "", ""),
    ]
    query = "What is the total cost of my new tennis gear?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$225" in res

def test_multi_session_camping_duration():
    candidates = [
        ("mem1", "I spent 3 days camping in Yosemite National Park.", "", "", ""),
        ("mem2", "We went on a 5-day camping trip in Zion.", "", "", ""),
    ]
    query = "How many days did I spend on camping trips in the United States this year?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "8 days" in res


def test_multi_session_model_kits_count():
    candidates = [
        ("mem1", "I bought a Revell F-15 Eagle model kit.", "", "", ""),
        ("mem2", "I finished the Tamiya Spitfire model kit.", "", "", ""),
        ("mem3", "I worked on my Tiger I tank model kit.", "", "", ""),
    ]
    query = "How many model kits have I worked on or bought?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "3 model kits" in res or "3" in res

def test_social_media_breaks_duration():
    candidates = [
        ("mem1", "I actually just got back from a 10-day break in mid-February.", "", "", ""),
        ("mem2", "I even took a week-long break from it in mid-January, and it was really refreshing.", "", "", ""),
    ]
    query = "How many days did I take social media breaks in total?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "17 days" in res


def test_movie_festivals_entity_count():
    candidates = [
        ("mem1", "I participated in the 48-hour film challenge at the Austin Film Festival... screening at the Seattle International Film Festival", "", "", ""),
        ("mem2", "I volunteered at the Portland Film Festival, where I helped with event coordination", "", "", ""),
        ("mem3", "I attended a screening of Joker at AFI Fest in LA", "", "", ""),
    ]
    query = "How many movie festivals that I attended?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "4" in res

def test_relative_temporal_delta_with_distractor_dates():
    as_of = datetime.fromisoformat("2023-04-20T10:12:00+00:00").timestamp()
    candidates = [
        ("distractor_1", "Some other event occurred on 2023-04-15", "", "", "2023-04-15T12:00:00Z"),
        ("answer_1", "I catch up with Emma over lunch today", "", "", "2023-04-11T23:18:00Z"),
    ]
    query = "How many days ago did I meet Emma?"
    res = calculate_temporal_delta(query, candidates, as_of=as_of)
    assert res is not None
    assert "9 days" in res


def test_chronological_ordering_solver():
    candidates = [
        ("mem1", "I visited the Museum of History today.", "", "", "2023-02-15T10:00:00Z"),
        ("mem2", "I visited the Science Museum today.", "", "", "2023-01-10T10:00:00Z"),
        ("mem3", "I visited the Modern Art Museum today.", "", "", "2023-03-01T10:00:00Z"),
    ]
    query = "What is the order of the museums I visited from earliest to latest?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "Science Museum, Museum of History, Modern Art Museum" in res


def test_comparative_entity_difference():
    candidates = [
        ("mem1", "I booked a luxurious resort in Maui that costs over $300 per night.", "", "", ""),
        ("mem2", "I stayed in a hostel in Tokyo that cost around $30 per night.", "", "", ""),
    ]
    query = "How much more did I spend on accommodations per night in Hawaii compared to Tokyo?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$270" in res


def test_multi_entity_weeks_duration():
    candidates = [
        ("mem1", "I watched all 22 Marvel Cinematic Universe movies in two weeks.", "", "", ""),
        ("mem2", "I watched all main Star Wars films in 1.5 weeks.", "", "", ""),
    ]
    query = "How many weeks did it take me to watch all the Marvel Cinematic Universe movies and the main Star Wars films?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "3.5 weeks" in res


def test_open_charity_and_market_sales_accumulation():
    charity_candidates = [
        ("mem1", "I recently participated in a charity walk and managed to raise $250 through sponsors.", "", "", ""),
        ("mem2", "I recently participated in a Bike-a-Thon for Cancer Research and my team managed to raise $5,000!", "", "", ""),
        ("mem3", "I just helped organize a charity yoga event that raised $600 for a local animal shelter.", "", "", ""),
    ]
    charity_query = "How much money did I raise in total through all the charity events I participated in?"
    res_charity = extract_and_aggregate_quantities(charity_query, charity_candidates)
    assert res_charity is not None
    assert "$5,850" in res_charity

    market_candidates = [
        ("mem1", "I sold 12 bunches of fresh organic herbs at the farmers' market, earning a total of $120.", "", "", ""),
        ("mem2", "I sold 15 jars of homemade jam at the Homemade Market, earning $225.", "", "", ""),
        ("mem3", "I sold 20 potted herb plants at the Summer Solstice Market for $7.5 each.", "", "", ""),
    ]
    market_query = "What is the total amount of money I earned from selling my products at the markets?"
    res_market = extract_and_aggregate_quantities(market_query, market_candidates)
    assert res_market is not None
    assert "$495" in res_market
