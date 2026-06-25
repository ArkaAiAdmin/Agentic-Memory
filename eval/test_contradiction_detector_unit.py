#!/usr/bin/env python3
"""Unit tests for contradiction_detector.py.

Targets the pure, in-memory public surface:
  * ``NEGATION_PAIRS`` data integrity
  * ``STOP_WORDS`` shape
  * ``significant_words`` (filters stop words, lowercases)
  * ``classify_operation`` (heuristic on title_slug)
  * ``split_segments`` / ``split_sentences`` (string splitting)
  * ``detect_contradictions`` (the main entry point, but it needs a
    live DB so we exercise only its data-table and DB-less path here).

We do NOT load the embedding model for semantic detection. The
semantic path is exercised end-to-end by the integration suite.
"""

import os
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


class TestNegationPairsData(unittest.TestCase):
    def test_pairs_are_tuples(self):
        from contradiction_detector import NEGATION_PAIRS

        for p in NEGATION_PAIRS:
            self.assertIsInstance(p, tuple)
            self.assertEqual(len(p), 2)

    def test_pairs_have_distinct_pos_and_neg(self):
        from contradiction_detector import NEGATION_PAIRS

        for pos, neg in NEGATION_PAIRS:
            self.assertNotEqual(
                pos, neg, f"pair has identical pos/neg: {pos!r} vs {neg!r}"
            )

    def test_known_pair_present(self):
        from contradiction_detector import NEGATION_PAIRS

        flat = {w for pair in NEGATION_PAIRS for w in pair}
        self.assertIn("enabled", flat)
        self.assertIn("disabled", flat)
        self.assertIn("safe", flat)
        self.assertIn("unsafe", flat)


class TestStopWords(unittest.TestCase):
    def test_stop_words_is_frozenset(self):
        from contradiction_detector import STOP_WORDS

        self.assertIsInstance(STOP_WORDS, frozenset)

    def test_common_words_present(self):
        from contradiction_detector import STOP_WORDS

        for w in ("a", "the", "is", "and", "to"):
            self.assertIn(w, STOP_WORDS)

    def test_meaningful_words_absent(self):
        from contradiction_detector import STOP_WORDS

        for w in ("python", "memory", "database", "agent", "search"):
            self.assertNotIn(w, STOP_WORDS)


class TestSignificantWords(unittest.TestCase):
    def test_empty_string_returns_empty(self):
        from contradiction_detector import significant_words

        self.assertEqual(significant_words(""), set())

    def test_drops_stop_words(self):
        from contradiction_detector import significant_words

        # Note: "fox" is 3 chars and is filtered by MIN_WORD_LEN; we use
        # 4+ char words so length filter doesn't dominate the test.
        out = significant_words("the quick brown elephant jumps")
        self.assertIn("quick", out)
        self.assertIn("brown", out)
        self.assertIn("elephant", out)
        self.assertIn("jumps", out)
        self.assertNotIn("the", out)

    def test_lowercases_input(self):
        from contradiction_detector import significant_words

        out = significant_words("Python PYTHON python")
        # All three collapse to the same word.
        self.assertEqual(len(out), 1)
        self.assertIn("python", out)

    def test_short_words_filtered(self):
        from contradiction_detector import significant_words

        out = significant_words("a b c defg hijk")
        # Words under MIN_WORD_LEN=4 are filtered.
        self.assertNotIn("a", out)
        self.assertNotIn("b", out)
        self.assertNotIn("c", out)
        self.assertIn("defg", out)
        self.assertIn("hijk", out)


class TestClassifyOperation(unittest.TestCase):
    """classify_operation(new, existing) returns (op, reason). op is
    one of ADD / UPDATE / DELETE / NOOP. The detector looks at the
    *content* (not slugs) for signals like 'supersedes', 'no longer'."""

    def test_add_when_no_existing(self):
        from contradiction_detector import classify_operation

        op, reason = classify_operation("new content", "")
        self.assertEqual(op, "ADD")
        self.assertIn("existing", reason.lower())

    def test_noop_when_identical(self):
        from contradiction_detector import classify_operation

        op, _ = classify_operation("same text", "same text")
        self.assertEqual(op, "NOOP")

    def test_delete_marker(self):
        from contradiction_detector import classify_operation

        op, _ = classify_operation("[DELETED]", "old text")
        self.assertEqual(op, "DELETE")

    def test_delete_on_empty_new(self):
        from contradiction_detector import classify_operation

        op, _ = classify_operation("", "old text")
        self.assertEqual(op, "DELETE")

    def test_update_on_supersedes_signal(self):
        from contradiction_detector import classify_operation

        op, _ = classify_operation(
            "This supersedes the old approach", "the old approach"
        )
        self.assertEqual(op, "UPDATE")

    def test_update_on_high_overlap(self):
        from contradiction_detector import classify_operation

        # Most words overlap → UPDATE.
        op, _ = classify_operation(
            "the quick brown fox jumps over the lazy dog",
            "the quick brown fox runs over the lazy dog",
        )
        self.assertEqual(op, "UPDATE")

    def test_add_on_distinct(self):
        from contradiction_detector import classify_operation

        op, _ = classify_operation(
            "completely unrelated content here please",
            "the old note about something else entirely",
        )
        self.assertEqual(op, "ADD")


class TestSplitSentences(unittest.TestCase):
    def test_empty_string(self):
        from contradiction_detector import split_sentences

        self.assertEqual(split_sentences(""), [])

    def test_single_sentence(self):
        from contradiction_detector import split_sentences

        out = split_sentences("This is one sentence.")
        self.assertEqual(len(out), 1)
        self.assertIn("one sentence", out[0])

    def test_multiple_sentences(self):
        from contradiction_detector import split_sentences

        out = split_sentences("First sentence. Second sentence. Third.")
        self.assertGreaterEqual(len(out), 2)
        joined = " ".join(out)
        self.assertIn("First", joined)
        self.assertIn("Second", joined)

    def test_handles_exclamation_and_question(self):
        from contradiction_detector import split_sentences

        out = split_sentences("Wow! Really? Yes.")
        self.assertGreaterEqual(len(out), 2)


class TestSplitSegments(unittest.TestCase):
    """split_segments(text) returns list of (segment_text, kind)
    tuples where kind is one of 'header' | 'list' | 'code' | 'prose'."""

    def test_empty(self):
        from contradiction_detector import split_segments

        self.assertEqual(split_segments(""), [])

    def test_single_segment_returns_tuple(self):
        from contradiction_detector import split_segments

        out = split_segments("just one segment")
        self.assertEqual(len(out), 1)
        text, kind = out[0]
        self.assertEqual(text, "just one segment")
        self.assertIn(kind, ("header", "list", "code", "prose"))

    def test_splits_on_blank_lines(self):
        from contradiction_detector import split_segments

        out = split_segments("para one\n\npara two")
        self.assertEqual(len(out), 2)
        texts = [seg[0] for seg in out]
        self.assertIn("para one", texts)
        self.assertIn("para two", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
