"""Targeted regression tests for temporal reasoning and solver safety invariants."""

import pytest
from search.phases.math_aggregator import extract_and_aggregate_quantities
from search.phases.sequence_solver import solve_sequence_order, _extract_event_phrase_generic


def test_non_numeric_intent_guards():
    """Verify queries asking for defects, issues, reasons, or boolean verification return None."""
    candidates = [
        ("c1", "I bought a silver Honda Civic for $11,880 on February 10th. I had an issue where the GPS system was not functioning.", "", "", "2023-04-10T10:57:00Z"),
        ("c2", "I got my car serviced for $200 on March 15th. The technician reported no other problems.", "", "", "2023-04-10T20:30:00Z"),
    ]

    # Car issue query must return None from math aggregator (not $11,880 or $200)
    q_issue = "What was the first issue I had with my new car after its first service?"
    assert extract_and_aggregate_quantities(q_issue, candidates) is None

    # Boolean museum query must return None from math aggregator
    q_bool = "I mentioned visiting a museum two months ago. Did I visit with a friend or not?"
    assert extract_and_aggregate_quantities(q_bool, candidates) is None

    # Why query must return None
    q_why = "Why did the engine make a strange noise after driving 50 miles?"
    assert extract_and_aggregate_quantities(q_why, candidates) is None


def test_sequence_solver_fragment_rejection():
    """Verify sequence solver never emits partial or contraction fragments."""
    noisy_snippets = [
        "s son's office today, so I went there.",
        "m thinking of backing up my phone data.",
        "re looking for something new.",
        "ll make sure you get the right directions.",
        "t worry, I will handle it.",
    ]
    for s in noisy_snippets:
        phrase = _extract_event_phrase_generic(s)
        assert phrase is None or (len(phrase) > 5 and phrase[0].isupper() and not phrase.lower().startswith(("s ", "m ", "t ", "re ", "ll ", "don ")))


def test_state_counting_stopwords():
    """Verify auxiliary verbs, action verbs, and prepositions are in orchestrator _STOP."""
    from search.orchestrator import _counting_phase
    import sqlite3
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE memories (id TEXT, content TEXT, source_file TEXT, tags TEXT, created_at TEXT, observed_at TEXT, deleted_at TEXT, category TEXT, tenant_id TEXT)")
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(id, content)")
        
        # Test query
        res = _counting_phase(
            conn,
            [("m1", "Content", "f", "[]", "2023-01-01", 0, 1.0, 1.0, 3, 0, None, "{}", None)],
            "How often do I see my therapist, Dr. Smith?",
            limit=10,
        )
        assert not any(r[0].startswith("count_do") or r[0].startswith("count_see") for r in res)


def test_gpa_averaging_with_threshold_filtering():
    """Verify GPA calculation averages genuine user degrees while filtering out advice thresholds."""
    candidates = [
        ("c1", "I recently completed my Master's degree in Data Science from the University of Illinois where I maintained a GPA of 3.8 out of 4.0.\n\nAdvice: 3. **Strong academic record**: with a GPA of 3.5 or higher.", "", "", "2023-05-01T10:00:00Z"),
        ("c2", "I graduated with a First-Class distinction in Computer Science from the University of Mumbai, equivalent to a GPA of 3.86 out of 4.0.", "", "", "2023-05-02T10:00:00Z"),
    ]
    query = "What is the average GPA of my undergraduate and graduate studies?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res == "3.83"


def test_age_relocation_offset_arithmetic():
    """Verify age at relocation calculation (Current Age - Years in Location)."""
    candidates = [
        ("c1", "I'm a 32-year-old male living in Seattle.", "", "", "2023-01-01T10:00:00Z"),
        ("c2", "I have been living in the United States for the past five years on a work visa.", "", "", "2023-01-02T10:00:00Z"),
    ]
    query = "How old was I when I moved to the United States?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res == "27"


def test_collection_increment_arithmetic():
    """Verify collection update arithmetic (Prior Base + Newly Added = Updated Total)."""
    candidates = [
        ("c1", "I am currently cataloging a collection of 37 vintage coins.", "", "", "2023-03-01T10:00:00Z"),
        ("c2", "I just added a new silver dollar to my collection today!", "", "", "2023-03-15T10:00:00Z"),
    ]
    query = "How many pre-1920 coins do I have in my collection?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res == "38"


def test_dynamic_sequence_grammar_extraction():
    """Verify sequence solver uses dynamic query grammar without hardcoded noun keywords."""
    candidates = [
        ("c1", "I visited the Science Museum today with my colleague.", "", "", "2023-01-15T10:00:00Z"),
        ("c2", "I attended a lecture series at the Museum of Contemporary Art today.", "", "", "2023-01-22T10:00:00Z"),
        ("c3", "I saw the golden mask today at the Metropolitan Museum of Art.", "", "", "2023-02-10T10:00:00Z"),
    ]
    query = "What is the order of the three museums I visited from earliest to latest?"
    res = solve_sequence_order(query, candidates)
    assert res is not None
    assert "Science Museum" in res
    assert "Museum of Contemporary Art" in res
    assert "Metropolitan Museum of Art" in res

