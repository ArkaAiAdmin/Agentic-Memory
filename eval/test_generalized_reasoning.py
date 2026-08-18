"""Unit tests for generalized reasoning and numeric aggregation."""

from search.phases.math_aggregator import extract_and_aggregate_quantities, extract_query_unit


def test_generalized_sports_gear_sum():
    """Verify numeric sum across multiple gear purchases."""
    candidates = [
        ("mem1", "I bought a tennis racket for $150.", "", "", ""),
        ("mem2", "I spent $50 on shoes and $25 on balls.", "", "", ""),
    ]
    query = "What is the total cost of my new tennis gear?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$225" in res


def test_generalized_charity_fundraising_sum():
    """Verify charity fundraising accumulation across sessions."""
    charity_candidates = [
        ("mem1", "I recently participated in a charity walk and managed to raise $250 through sponsors.", "", "", ""),
        ("mem2", "I recently participated in a Bike-a-Thon for Cancer Research and my team managed to raise $5,000!", "", "", ""),
        ("mem3", "I just helped organize a charity yoga event that raised $600 for a local animal shelter.", "", "", ""),
    ]
    charity_query = "How much money did I raise in total through all the charity events I participated in?"
    res_charity = extract_and_aggregate_quantities(charity_query, charity_candidates)
    assert res_charity is not None
    assert "$5,850" in res_charity


def test_generalized_gpa_average_calculation():
    """Verify GPA average calculation across academic levels."""
    candidates = [
        ("c1", "I completed my bachelor degree with an undergraduate GPA of 3.86 from Mumbai University."),
        ("c2", "Later, I graduated with a master degree and a graduate GPA of 3.80 from University of Illinois."),
    ]
    query = "What is the average GPA of my undergraduate and graduate degrees?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert res == "3.83"


def test_generalized_pages_remaining_subtraction():
    """Verify pages remaining subtraction for a book."""
    candidates = [
        ("c1", "I started reading The Nightingale, which is a novel with 440 pages total."),
        ("c2", "I am currently on page 250 of The Nightingale."),
    ]
    query = "How many pages do I have left to read in 'The Nightingale'?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert res == "190"


def test_unit_query_guard_non_currency():
    """Verify hours/weeks queries are not mistaken for currency."""
    unit, is_curr = extract_query_unit("How many hours have I spent playing video games?")
    assert unit == "hour"
    assert is_curr is False

    unit2, is_curr2 = extract_query_unit("How many weeks did it take to build the project?")
    assert unit2 == "week"
    assert is_curr2 is False
