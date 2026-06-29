"""Sprint 4: heuristic decision-extraction tests.

All tests are self-contained — no DB required for the extraction logic.
Integration with SessionManager is tested via the existing session_manager
tests (Sprint 2) and the hook-path integration test (Sprint 3).
"""

from __future__ import annotations

import json
import os

import pytest

from save.decision_extraction import (
    DECISION_CATEGORIES,
    _extract_decision_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _c(content: str, category: str = "decisions") -> list:
    return _extract_decision_candidates(content, category)


def _titles(candidates: list) -> list[str]:
    return [c.title for c in candidates]


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


class TestFirstPersonDecision:
    def test_i_decided(self):
        c = _c("I decided to use SQLite for the local store.")
        assert len(c) >= 1
        assert "decided" in c[0].claim.lower() or "decided" in c[0].title.lower()
        assert c[0].confidence >= 0.7

    def test_we_chose(self):
        c = _c("We chose PostgreSQL for the production cluster.")
        assert len(c) >= 1
        assert c[0].confidence >= 0.7

    def test_went_with(self):
        c = _c("The team went with a background worker.")
        assert len(c) >= 1

    def test_opted_for(self):
        c = _c("I opted for the simpler merge strategy.")
        assert len(c) >= 1


class TestADRMarkers:
    def test_adr_header(self):
        c = _c("# ADR-012: Use event sourcing for audit trail")
        assert len(c) >= 1
        assert "ADR" in c[0].title

    def test_adr_body(self):
        c = _c("""# ADR-007

We decided to use SQLite WAL mode.

**Decision:** Enable WAL for better concurrency.
""")
        assert len(c) >= 1


class TestRFCMarkers:
    def test_rfc_heading(self):
        c = _c("## RFC: Unified search API")
        # RFC markers produce lower-confidence "question" events
        assert len(c) >= 1


class TestTradeoffTables:
    def test_option_table_header(self):
        c = _c("""
| Option | Latency | Cost |
|--------|---------|------|
| A      | 10ms    | $5   |
| B      | 50ms    | $1   |
""")
        assert len(c) >= 1
        assert c[0].event_type == "evidence"
        assert c[0].confidence >= 0.5


class TestResolutionMarkers:
    def test_bold_decision(self):
        c = _c("**Decision:** Use Redis for caching.")
        assert len(c) >= 1
        assert c[0].confidence >= 0.7

    def test_arrow_chosen(self):
        c = _c("After evaluation → chosen: PostgreSQL.")
        assert len(c) >= 1


# ---------------------------------------------------------------------------
# Category gating
# ---------------------------------------------------------------------------


class TestCategoryGating:
    def test_decisions_category_extracts(self):
        assert len(_c("I decided to fix this bug.", "decisions")) >= 1

    def test_lessons_category_extracts(self):
        assert len(_c("We decided to retry on timeout.", "lessons")) >= 1

    def test_projects_category_extracts(self):
        assert len(_c("The team chose the MVP scope.", "projects")) >= 1

    def test_architecture_category_extracts(self):
        assert len(_c("I went with the event-driven approach.", "architecture")) >= 1

    def test_non_decision_category_skips(self):
        assert len(_c("I decided to fix this bug.", "preferences")) == 0
        assert len(_c("I decided to fix this bug.", "todos")) == 0


# ---------------------------------------------------------------------------
# False-positive rejection
# ---------------------------------------------------------------------------


class TestFalsePositiveRejection:
    def test_empty_content(self):
        assert _c("") == []
        assert _c("   ") == []

    def test_whitespace_only(self):
        assert _c("\n\n  \t") == []

    def test_no_patterns(self):
        text = "The quick brown fox jumps over the lazy dog. " * 5
        assert _c(text) == []

    def test_question_marks_only(self):
        text = "What? Really? Are you sure? " * 10
        assert _c(text) == []


# ---------------------------------------------------------------------------
# Thread slug stability
# ---------------------------------------------------------------------------


class TestThreadSlug:
    def test_same_title_produces_same_slug(self):
        a = _extract_decision_candidates("I decided to use Redis.", "decisions")
        b = _extract_decision_candidates("I decided to use Redis.", "decisions")
        assert a[0].thread_slug == b[0].thread_slug

    def test_different_titles_produce_different_slugs(self):
        a = _extract_decision_candidates("I decided to use PostgreSQL.", "decisions")
        b = _extract_decision_candidates("I decided to use SQLite.", "decisions")
        assert a[0].thread_slug != b[0].thread_slug


# ---------------------------------------------------------------------------
# Alternatives extraction (tradeoff tables)
# ---------------------------------------------------------------------------


class TestAlternativesExtraction:
    def test_bullet_list_as_alternatives(self):
        text = """
| Option | detail |
|--------|--------|
| A      | fast   |
| B      | cheap  |
- Option A: in-memory cache
- Option B: disk-backed store
- Option C: external Redis
"""
        c = _extract_decision_candidates(text, "decisions")
        assert len(c) >= 1
        assert len(c[0].alternatives) >= 1


# ---------------------------------------------------------------------------
# DECISION_CATEGORIES constant
# ---------------------------------------------------------------------------


class TestDecisionCategories:
    def test_expected_categories_present(self):
        for cat in ("decisions", "lessons", "projects", "architecture"):
            assert cat in DECISION_CATEGORIES

    def test_non_decision_categories_not_in_set(self):
        for cat in ("preferences", "todos", "journal", "daily"):
            assert cat not in DECISION_CATEGORIES


# ---------------------------------------------------------------------------
# Sprint 6: LLM enrichment (opt-in)
# ---------------------------------------------------------------------------

import os as _os

_SKIP_LLM = _os.environ.get("MEMORY_SESSION_DECISION_LLM") != "1"


@pytest.mark.skipif(
    _SKIP_LLM,
    reason="LLM extraction disabled (MEMORY_SESSION_DECISION_LLM=1 to enable)",
)
class TestLLMEnrichment:
    def test_enrich_returns_candidates(self):
        from save.decision_extraction import _enrich_candidates_with_llm, DecisionCandidate

        cands = [
            DecisionCandidate(
                title="Heuristic",
                claim="We chose X.",
                event_type="decision",
                confidence=0.7,
            )
        ]
        result = _enrich_candidates_with_llm(
            cands, "We decided to use PostgreSQL for the main database."
        )
        assert len(result) >= 1
        assert any(c.title for c in result)

    def test_enrich_does_not_fail_on_missing_llm(self):
        from save.decision_extraction import _enrich_candidates_with_llm, DecisionCandidate

        cands = [
            DecisionCandidate(
                title="Test",
                claim="chose option A",
                event_type="decision",
                confidence=0.5,
            )
        ]
        result = _enrich_candidates_with_llm(cands, "some content")
        assert isinstance(result, list)

    def test_enrich_empty_candidates_returns_empty(self):
        from save.decision_extraction import _enrich_candidates_with_llm

        result = _enrich_candidates_with_llm([], "some content")
        assert result == []
