"""Tests for Adaptive Tiered Retrieval — automatic deep_rerank triggering."""

from __future__ import annotations

from search.orchestrator import _COMPLEX_REASONING_RE, _LIST_ENUMERATION_RE


class TestAdaptiveTieredRetrieval:
    """Verify the regex patterns that trigger adaptive tiered retrieval."""

    def test_compare_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Compare the memory systems")

    def test_contrast_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Contrast approaches to caching")

    def test_why_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Why did the system fail?")

    def test_how_does_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("How does the search work?")

    def test_explain_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Explain the architecture")

    def test_audit_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Audit the security posture")

    def test_analyze_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Analyze performance bottlenecks")

    def test_summarize_all_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Summarize all sessions this week")

    def test_reconstruct_triggers_complex(self):
        assert _COMPLEX_REASONING_RE.search("Reconstruct the timeline")

    def test_simple_query_does_not_trigger(self):
        assert not _COMPLEX_REASONING_RE.search("find my notes about python")

    def test_list_query_does_not_trigger(self):
        assert not _COMPLEX_REASONING_RE.search("list memories")

    def test_list_enumeration_expands_limit(self):
        """List/sequence queries should expand the retrieval limit to 30."""
        assert _LIST_ENUMERATION_RE.search("list all sessions")
        assert _LIST_ENUMERATION_RE.search("sequence of events")
        assert _LIST_ENUMERATION_RE.search("steps in the process")
        assert _LIST_ENUMERATION_RE.search("phases of the project")
        assert _LIST_ENUMERATION_RE.search("chronological order")

    def test_non_list_query_does_not_expand(self):
        assert not _LIST_ENUMERATION_RE.search("what is the database password")

    def test_reconstruct_in_list_pattern(self):
        assert _LIST_ENUMERATION_RE.search("reconstruct the conversation")

    def test_mention_only_pattern(self):
        assert _LIST_ENUMERATION_RE.search("mention only the key decisions")

    def test_how_did_pattern(self):
        assert _LIST_ENUMERATION_RE.search("how did the incident unfold")
