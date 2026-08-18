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


def test_n_way_multi_entity_sum():
    candidates = [
        ("m1", "I spent 2 weeks reading 'The Nightingale'.", "", "", ""),
        ("m2", "I spent 4 weeks listening to 'Sapiens: A Brief History of Humankind'.", "", "", ""),
        ("m3", "I spent 2 weeks on 'The Power'.", "", "", ""),
    ]
    query = "How many weeks in total do I spend on reading 'The Nightingale' and listening to 'Sapiens: A Brief History of Humankind' and 'The Power'?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "8 weeks" in res


def test_compound_noun_multi_entity_cost():
    candidates = [
        ("m1", "I bought a premium car cover for $90.", "", "", ""),
        ("m2", "I picked up some detailing spray for $50.", "", "", ""),
    ]
    query = "What is the total cost of the car cover and detailing spray I purchased?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$140" in res


def test_driving_hours_duration_sum():
    candidates = [
        ("m1", "I took a road trip to the coast, which was a 4-hour drive.", "", "", ""),
        ("m2", "My second road trip to the mountains took 5 hours of driving.", "", "", ""),
        ("m3", "For the third road trip to the national park, it was a 6-hour drive each way.", "", "", ""),
    ]
    query = "How many hours in total did I spend driving to my three road trip destinations combined?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "15 hours" in res


def test_gaming_hours_duration_sum():
    candidates = [
        ("m1", "I logged 60 hours in Assassin's Creed over the past month.", "", "", ""),
        ("m2", "I played 80 hours of Elden Ring on my console.", "", "", ""),
    ]
    query = "How many hours have I spent playing games in total?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "140 hours" in res


def test_semantic_line_filtering_bike_expenses():
    candidates = [
        ("m1", "I took my bike for a tune-up which cost $50.", "", "", ""),
        ("m2", "I purchased a new cycling helmet for $60 from the local bike shop.", "", "", ""),
        ("m3", "I bought replacement bike lights and pedals for $75.", "", "", ""),
        ("m4", "Distractor session: company revenue grew by $34,000 in Q3 2023 with 500,000 active users.", "", "", ""),
    ]
    query = "How much total money have I spent on bike-related expenses since the start of the year?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$185" in res


def test_doctor_specialist_counter():
    candidates = [
        ("m1", "I went to my primary care doctor Dr. Smith for an annual checkup.", "", "", ""),
        ("m2", "I visited an ENT specialist for my sinus issues.", "", "", ""),
        ("m3", "I had an appointment with a dermatologist for a skin screening.", "", "", ""),
    ]
    query = "How many different doctors did I visit?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "3 different doctors" in res


def test_infix_since_when_temporal_delta():
    candidates = [
        ("m1", "[Session Date: 2023-02-01]\nI started taking ukulele lessons today!", "", "", "2023-02-01 10:00:00"),
        ("m2", "[Session Date: 2023-02-25]\nI decided to take my acoustic guitar to the guitar tech for servicing.", "", "", "2023-02-25 14:00:00"),
    ]
    query = "How many days had passed since I started taking ukulele lessons when I decided to take my acoustic guitar to the guitar tech for servicing?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "24 days" in res


def test_infix_before_did_temporal_delta():
    candidates = [
        ("m1", "[Session Date: 2023-05-01]\nI ordered her birthday gift online for my best friend.", "", "", "2023-05-01 10:00:00"),
        ("m2", "[Session Date: 2023-05-08]\nWe celebrated my best friend's birthday party tonight!", "", "", "2023-05-08 19:00:00"),
    ]
    query = "How many days before my best friend's birthday party did I order her gift?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "7 days" in res


def test_binary_precedence_solver():
    candidates = [
        ("m1", "[Session Date: 2023-04-10]\nI had a lovely lunch meeting with Rachel today.", "", "", "2023-04-10 12:00:00"),
        ("m2", "[Session Date: 2023-05-15]\nI attended the pride parade with my friends downtown!", "", "", "2023-05-15 12:00:00"),
    ]
    query = "Which event happened first, the meeting with Rachel or the pride parade?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "Meeting with rachel happened first" in res


def test_days_ago_relative_solver():
    as_of = datetime(2023, 5, 20, 12, 0, tzinfo=timezone.utc)
    candidates = [
        ("m1", "[Session Date: 2023-05-10]\nI bought a brand new smoker from Home Depot today.", "", "", "2023-05-10 10:00:00"),
    ]
    query = "How many days ago did I buy a smoker?"
    res = calculate_temporal_delta(query, candidates, as_of=as_of)
    assert res is not None
    assert "10 days" in res


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


def test_average_age_and_gpa_calculation():
    # Test average age
    candidates_age = [
        ("m1", "I just turned 32 on February 12th.", "", "", ""),
        ("m2", "My mom is 55 and my dad is 58.", "", "", ""),
        ("m3", "My grandma is 75 and my grandpa is 78.", "", "", ""),
    ]
    query_age = "What is the average age of me, my parents, and my grandparents?"
    res_age = extract_and_aggregate_quantities(query_age, candidates_age)
    assert res_age == "59.60" or res_age == "59.6"

    # Test average GPA
    candidates_gpa = [
        ("m1", "I completed my Master's degree in Data Science where I maintained a GPA of 3.86.", "", "", ""),
        ("m2", "My undergraduate GPA was 3.80 in Computer Science.", "", "", ""),
    ]
    query_gpa = "What is the average GPA of my undergraduate and graduate studies?"
    res_gpa = extract_and_aggregate_quantities(query_gpa, candidates_gpa)
    assert res_gpa == "3.83"


def test_cross_session_savings_and_price_difference():
    # Test cross-session savings
    candidates_diff = [
        ("m1", "I loved the designer handbag which had an original tag price of $500.", "", "", ""),
        ("m2", "I bought the designer handbag at TK Maxx for $200.", "", "", ""),
    ]
    query_diff = "How much did I save on the designer handbag at TK Maxx?"
    res_diff = extract_and_aggregate_quantities(query_diff, candidates_diff)
    assert res_diff == "$300"

    # Test extra payment
    candidates_pay = [
        ("m1", "The initial quote for the trip was $2,500.", "", "", ""),
        ("m2", "The final price for the trip ended up being $2,800.", "", "", ""),
    ]
    query_pay = "How much more did I have to pay for the trip after the initial quote?"
    res_pay = extract_and_aggregate_quantities(query_pay, candidates_pay)
    assert res_pay == "$300"


def test_conjunction_entities_summation():
    # Test car cover and detailing spray
    candidates_items = [
        ("m1", "I recently got a waterproof car cover for $120.", "", "", ""),
        ("m2", "I bought a bottle of car detailing spray for $20.", "", "", ""),
    ]
    query_items = "What is the total cost of the car cover and detailing spray I purchased?"
    res_items = extract_and_aggregate_quantities(query_items, candidates_items)
    assert res_items == "$140"

    # Test social media views
    candidates_views = [
        ("m1", "My TikTok video has 542 views.", "", "", ""),
        ("m2", "My YouTube video has 1,456 views.", "", "", ""),
    ]
    query_views = "What is the total number of views on my most popular videos on YouTube and TikTok?"
    res_views = extract_and_aggregate_quantities(query_views, candidates_views)
    assert "1,998" in res_views


def test_item_targeted_order_arrival_duration():
    from search.phases.temporal_delta_solver import calculate_temporal_delta

    candidates = [
        ("m1", "I ordered the new remote shutter release on February 5th.", "", "", "2023-02-05T10:00:00Z"),
        ("m2", "I just received my new remote shutter release on February 10th.", "", "", "2023-02-10T10:00:00Z"),
        ("m3", "I also bought a tripod on March 1st and received it on March 25th.", "", "", "2023-03-01T10:00:00Z"),
    ]
    query = "How many days did it take for me to receive the new remote shutter release after I ordered it?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "5 days" in res


def test_three_event_chronological_ordering():
    from search.phases.temporal_delta_solver import calculate_temporal_delta

    candidates = [
        ("m1", "I helped my cousin pick out stuff for her baby shower.", "", "", "2023-02-10T10:00:00Z"),
        ("m2", "I ordered a customized phone case for my friend's birthday.", "", "", "2023-02-20T10:00:00Z"),
        ("m3", "I helped my friend prepare the nursery.", "", "", "2023-02-05T10:00:00Z"),
    ]
    query = "Which three events happened in the order from first to last: the day I helped my friend prepare the nursery, the day I helped my cousin pick out stuff for her baby shower, and the day I ordered a customized phone case for my friend's birthday?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "First, I helped my friend prepare the nursery" in res
    assert "then I helped my cousin pick out stuff for her baby shower" in res
    assert "finally I ordered a customized phone case for my friend's birthday" in res


def test_relative_delta_with_as_of():
    from search.phases.temporal_delta_solver import calculate_temporal_delta
    from datetime import datetime, timezone

    candidates = [
        ("m1", "I attended a baking class at a local culinary school.", "", "", "2022-03-25T10:00:00Z"),
    ]
    as_of = datetime(2022, 4, 15, 18, 46, tzinfo=timezone.utc)
    query = "How many days ago did I attend a baking class at a local culinary school?"
    res = calculate_temporal_delta(query, candidates, as_of=as_of)
    assert res is not None
    assert "21 days" in res


