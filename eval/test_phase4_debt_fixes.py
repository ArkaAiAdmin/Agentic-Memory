#!/usr/bin/env python3
"""Phase 4 debt fixes validation tests — TD-20 (Jaccard dedup bug)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quality_gates import filter_results, _jaccard, _tokenize


class TestJaccardDedupFix:
    """TD-20: Verify near-duplicate detection uses correct key tracking."""

    def test_near_dedup_after_exact_dedup(self):
        """Exact dup removed first, then near-dup check uses correct accepted keys."""
        base = "Memory systems use vector embeddings for semantic search retrieval across notes"
        results = [
            {"id": "a", "content": base, "source": "test"},
            {"id": "b", "content": base, "source": "test"},  # exact dup of a
            {"id": "c", "content": "Memory systems use vector embeddings for semantic search retrieval across all notes", "source": "test"},  # near dup
            {"id": "d", "content": "Completely unrelated content about quantum physics and particle dynamics", "source": "test"},
        ]
        filtered, stats = filter_results(results)
        ids = [r["id"] for r in filtered]
        assert "a" in ids
        assert "b" not in ids
        assert "c" not in ids
        assert "d" in ids
        assert stats["reasons"].get("exact_duplicate", 0) >= 1
        assert stats["reasons"].get("near_duplicate", 0) >= 1

    def test_near_dedup_three_way(self):
        """Three near-identical results: first kept, second and third filtered."""
        base = "Memory systems use vector embeddings for semantic search retrieval across notes"
        results = [
            {"id": "x", "content": base, "source": "test"},
            {"id": "y", "content": "Memory systems use vector embeddings for semantic search retrieval across notes.", "source": "test"},
            {"id": "z", "content": "Memory systems use vector embeddings for semantic search retrieval across notes too", "source": "test"},
        ]
        filtered, stats = filter_results(results)
        ids = [r["id"] for r in filtered]
        assert ids == ["x"]

    def test_no_false_positives_on_distinct(self):
        """Distinct results should not be filtered."""
        results = [
            {"id": "1", "content": "Python is a programming language used for web development", "source": "test"},
            {"id": "2", "content": "Rust is a systems programming language focused on safety", "source": "test"},
            {"id": "3", "content": "JavaScript runs in web browsers for interactive applications", "source": "test"},
        ]
        filtered, stats = filter_results(results)
        assert len(filtered) == 3

    def test_jaccard_basic(self):
        """Basic Jaccard similarity check."""
        a = _tokenize("quick brown fox jumps over lazy dog")
        b = _tokenize("quick brown fox leaps over lazy dog")
        sim = _jaccard(a, b)
        # 6 shared / 8 union = 0.75
        assert 0.7 < sim < 1.0

    def test_jaccard_identical(self):
        """Identical token sets yield 1.0."""
        a = _tokenize("hello world foo bar baz")
        assert _jaccard(a, a) == 1.0

    def test_jaccard_disjoint(self):
        """Disjoint sets yield 0.0."""
        a = _tokenize("alpha beta gamma")
        b = _tokenize("delta epsilon zeta")
        assert _jaccard(a, b) == 0.0
