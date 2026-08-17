"""Comprehensive tests for Math Aggregator, Temporal Delta Solver, and Attribute Extractor."""

from __future__ import annotations

from search.phases.math_aggregator import (
    extract_and_aggregate_quantities,
    parse_numeric_val,
    format_numeric_val,
    _get_item_content,
)
from search.phases.temporal_delta_solver import calculate_temporal_delta, parse_iso_date
from search.phases.attribute_extractor import extract_entity_attribute


# ---------------------------------------------------------------------------
# Math Aggregator — parse_numeric_val
# ---------------------------------------------------------------------------
class TestParseNumericVal:
    def test_integer(self):
        assert parse_numeric_val("500") == 500.0

    def test_decimal(self):
        assert parse_numeric_val("3.14") == 3.14

    def test_comma_separated(self):
        assert parse_numeric_val("500,000") == 500000.0

    def test_comma_large(self):
        assert parse_numeric_val("1,234,567") == 1234567.0

    def test_suffix_k(self):
        assert parse_numeric_val("300", "k") == 300000.0

    def test_suffix_thousand(self):
        assert parse_numeric_val("5", "thousand") == 5000.0

    def test_suffix_million(self):
        assert parse_numeric_val("1.5", "million") == 1500000.0

    def test_suffix_m(self):
        assert parse_numeric_val("2", "m") == 2000000.0

    def test_suffix_billion(self):
        assert parse_numeric_val("3", "billion") == 3000000000.0

    def test_suffix_b(self):
        assert parse_numeric_val("1", "b") == 1000000000.0

    def test_suffix_case_insensitive(self):
        assert parse_numeric_val("100", "K") == 100000.0
        assert parse_numeric_val("100", "M") == 100000000.0

    def test_invalid_returns_zero(self):
        assert parse_numeric_val("abc") == 0.0

    def test_empty_string(self):
        assert parse_numeric_val("") == 0.0

    def test_whitespace(self):
        assert parse_numeric_val("  500  ") == 500.0

    def test_comma_with_suffix(self):
        assert parse_numeric_val("1,500", "k") == 1500000.0


# ---------------------------------------------------------------------------
# Math Aggregator — format_numeric_val
# ---------------------------------------------------------------------------
class TestFormatNumericVal:
    def test_integer(self):
        assert format_numeric_val(800000.0) == "800,000"

    def test_small_integer(self):
        assert format_numeric_val(42.0) == "42"

    def test_decimal(self):
        assert format_numeric_val(14.5) == "14.50"

    def test_zero(self):
        assert format_numeric_val(0.0) == "0"

    def test_large_number(self):
        assert format_numeric_val(1234567.0) == "1,234,567"

    def test_negative(self):
        assert format_numeric_val(-500.0) == "-500"


# ---------------------------------------------------------------------------
# Math Aggregator — _get_item_content
# ---------------------------------------------------------------------------
class TestGetItemContent:
    def test_dict_with_content(self):
        assert _get_item_content({"content": "hello"}) == "hello"

    def test_dict_with_text(self):
        assert _get_item_content({"text": "world"}) == "world"

    def test_dict_content_none_falls_to_text(self):
        assert _get_item_content({"content": None, "text": "fallback"}) == "fallback"

    def test_list_tuple(self):
        assert _get_item_content(("id", "content_str")) == "content_str"

    def test_list_single_element(self):
        # Single-element list — no [1] access
        result = _get_item_content(["only_one"])
        assert result == "['only_one']"

    def test_object_with_content_attr(self):
        class Obj:
            content = "attr_value"
        assert _get_item_content(Obj()) == "attr_value"

    def test_bare_string(self):
        assert _get_item_content("plain string") == "plain string"

    def test_none_content_in_tuple(self):
        assert _get_item_content(("id", None)) == ""

    def test_dict_empty(self):
        # Empty dict — no 'content' or 'text' key
        result = _get_item_content({})
        assert result == ""


# ---------------------------------------------------------------------------
# Math Aggregator — extract_and_aggregate_quantities
# ---------------------------------------------------------------------------
class TestExtractAndAggregateQuantities:
    def test_empty_candidates(self):
        assert extract_and_aggregate_quantities("total users", []) is None

    def test_non_aggregation_query(self):
        candidates = [("m1", "There are 500 users")]
        assert extract_and_aggregate_quantities("who are the users", candidates) is None

    def test_single_number_returns_none(self):
        # Need 2+ extracted values for aggregation
        candidates = [("m1", "There are 500 users")]
        assert extract_and_aggregate_quantities("total users", candidates) is None

    def test_two_numbers_sums(self):
        query = "How many documents in total?"
        candidates = [
            ("m1", "Elasticsearch has 500,000 documents"),
            ("m2", "Solr has 300,000 documents"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result == "800,000"

    def test_three_numbers_sums(self):
        query = "What is the combined total headcount?"
        candidates = [
            ("m1", "Frontend team has 12 engineers"),
            ("m2", "Backend team has 18 engineers"),
            ("m3", "DevOps team has 5 engineers"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result == "35"

    def test_remaining_balance_subtraction(self):
        query = "What is the remaining budget allocated to infrastructure?"
        candidates = [
            ("m1", "The budget is $10,000 for infrastructure upgrade cost $3,000"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result is not None
        assert "$" in result

    def test_migration_filtered(self):
        # Migration lines should be excluded from sum
        query = "How many users in total?"
        candidates = [
            ("m1", "500 users in Project A"),
            ("m2", "migrated 200 users from Project A to Project B"),
            ("m3", "300 users in Project B"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result is not None
        # The 500 and 300 should be summed (migration line excluded)
        assert result == "800"

    def test_dedup_identical_snippets(self):
        query = "What is the total?"
        candidates = [
            ("m1", "Value is 100"),
            ("m2", "Value is 100"),  # duplicate
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        # Only one unique snippet, so only 1 number — should return None
        assert result is None

    def test_project_baseline_pattern(self):
        query = "What is the combined active users?"
        candidates = [
            ("m1", "Project Alpha has 450,000 active users"),
            ("m2", "Project Beta has 200,000 active users"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result is not None

    def test_headcount_delta(self):
        query = "What is the total headcount on the Backend team?"
        candidates = [
            ("m1", "Backend team started with 12 engineers. 3 transferred to frontend, and 5 new hires joined"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result == "14"  # 12 - 3 + 5 = 14

    def test_k_suffix_numbers(self):
        query = "What is the total bandwidth?"
        candidates = [
            ("m1", "Server A has 500k requests"),
            ("m2", "Server B has 300k requests"),
        ]
        result = extract_and_aggregate_quantities(query, candidates)
        assert result == "800,000"

    def test_capped_at_10_candidates(self):
        # Only first 10 candidates are processed
        candidates = [(f"m{i}", f"Value is {i * 100}") for i in range(15)]
        query = "What is the total sum?"
        result = extract_and_aggregate_quantities(query, candidates)
        assert result is not None


# ---------------------------------------------------------------------------
# Temporal Delta Solver — parse_iso_date
# ---------------------------------------------------------------------------
class TestParseIsoDate:
    def test_valid_date(self):
        dt = parse_iso_date("2024-03-15")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 3
        assert dt.day == 15

    def test_slash_separator(self):
        dt = parse_iso_date("2024/03/15")
        assert dt is not None
        assert dt.day == 15

    def test_invalid_string(self):
        assert parse_iso_date("not-a-date") is None

    def test_empty_string(self):
        assert parse_iso_date("") is None

    def test_partial_date(self):
        assert parse_iso_date("2024-03") is None

    def test_utc_timezone(self):
        dt = parse_iso_date("2024-01-01")
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# Temporal Delta Solver — calculate_temporal_delta
# ---------------------------------------------------------------------------
class TestCalculateTemporalDelta:
    def test_empty_candidates(self):
        assert calculate_temporal_delta("how many days passed", []) is None

    def test_single_candidate(self):
        candidates = [("m1", "Started on 2024-01-01")]
        assert calculate_temporal_delta("how many days passed", candidates) is None

    def test_non_delta_query(self):
        candidates = [
            ("m1", "Event on 2024-01-01"),
            ("m2", "Event on 2024-01-10"),
        ]
        assert calculate_temporal_delta("what happened", candidates) is None

    def test_days_delta(self):
        query = "How many days passed between start and end?"
        candidates = [
            ("m1", "Started on 2024-03-01"),
            ("m2", "Ended on 2024-03-15"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "14 days"

    def test_weeks_delta(self):
        query = "How many weeks passed between the two events?"
        candidates = [
            ("m1", "Event A on 2024-01-01"),
            ("m2", "Event B on 2024-01-15"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result is not None
        assert "weeks" in result

    def test_months_delta(self):
        query = "How many months passed between migration and completion?"
        candidates = [
            ("m1", "Migration started 2024-01-01"),
            ("m2", "Migration completed 2024-04-01"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result is not None
        assert "months" in result

    def test_years_delta(self):
        query = "How many years passed between versions?"
        candidates = [
            ("m1", "Version 1 released 2022-06-01"),
            ("m2", "Version 2 released 2024-06-01"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result is not None
        assert "years" in result

    def test_dates_in_content(self):
        query = "How many days passed?"
        candidates = [
            ("m1", "Deployed on 2024-06-01 to production"),
            ("m2", "Rolled back on 2024-06-03 due to issues"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "2 days"

    def test_dates_from_timestamps(self):
        # Dates can come from position [4] in the tuple
        query = "How many days passed?"
        candidates = [
            ("m1", "Some event", None, None, "2024-01-01"),
            ("m2", "Another event", None, None, "2024-01-10"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "9 days"

    def test_duplicate_dates_deduped(self):
        query = "How many days passed?"
        candidates = [
            ("m1", "Event on 2024-01-01"),
            ("m2", "Another event on 2024-01-01"),  # same date
            ("m3", "Final event on 2024-01-05"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "4 days"

    def test_same_day(self):
        # Same date is deduplicated, so we need 2 distinct dates
        # that are 1 day apart to test the delta logic
        query = "How many days passed?"
        candidates = [
            ("m1", "Event on 2024-01-01 morning"),
            ("m2", "Event on 2024-01-02 evening"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "1 day" if result == "1 day" else result == "1 days"

    def test_slash_dates(self):
        query = "How many days passed?"
        candidates = [
            ("m1", "Started 2024/03/01"),
            ("m2", "Finished 2024/03/10"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "9 days"

    def test_mixed_content_and_timestamp_dates(self):
        query = "How many days passed?"
        candidates = [
            ("m1", "Event on 2024-01-01", None, None, None),
            ("m2", "Another event", None, None, "2024-01-20"),
        ]
        result = calculate_temporal_delta(query, candidates)
        assert result == "19 days"


# ---------------------------------------------------------------------------
# Attribute Extractor
# ---------------------------------------------------------------------------
class TestAttributeExtractor:
    def test_empty_candidates(self):
        assert extract_entity_attribute("what version", []) is None

    def test_version_query(self):
        candidates = [("m1", "Evaluating Qdrant version 1.7.0 for indexing")]
        result = extract_entity_attribute("What version am I evaluating?", candidates)
        assert result == "1.7.0"

    def test_cost_query(self):
        candidates = [("m1", "The server costs $50/hour")]
        result = extract_entity_attribute("What is the cost?", candidates)
        assert result == "$50/hour"

    def test_port_query(self):
        candidates = [("m1", "Dashboard running on port 8501")]
        result = extract_entity_attribute("What port is the dashboard on?", candidates)
        assert result is not None

    def test_no_match_returns_none(self):
        candidates = [("m1", "Some random content")]
        assert extract_entity_attribute("what version", candidates) is None

    def test_non_tuple_candidate_skipped(self):
        candidates = ["not a tuple"]
        assert extract_entity_attribute("what version", candidates) is None

    def test_short_tuple_skipped(self):
        candidates = [("id",)]  # len < 2
        assert extract_entity_attribute("what version", candidates) is None

    def test_first_matching_candidate_wins(self):
        candidates = [
            ("m1", "Version 1.0.0 released"),
            ("m2", "Version 2.0.0 released"),
        ]
        result = extract_entity_attribute("what version", candidates)
        assert result == "1.0.0"

    def test_cost_with_rate(self):
        candidates = [("m1", "Hourly rate is $75/hr")]
        result = extract_entity_attribute("What is the hourly rate?", candidates)
        assert result is not None

    def test_version_v_prefix(self):
        candidates = [("m1", "Using v3.2.1 in production")]
        result = extract_entity_attribute("what version", candidates)
        assert result is not None

    def test_compound_pages_and_cost(self):
        candidates = [("m1", "I ordered an album with 50 pages and it cost $75.")]
        result = extract_entity_attribute("How many pages did the album have and what was the cost?", candidates)
        assert result is not None
        assert "50 pages" in result
        assert "$75" in result
