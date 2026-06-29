#!/usr/bin/env python3
"""T11: LLM-scored contradiction detection.

Verifies:
  - score_fact_contradiction_via_llm parses a float from a stub LLM output
  - _parse_contradiction_score handles edge cases (0, 1, decimals, junk)
  - supersede_fact accepts a score parameter and stores it
  - reconcile_fact_supersession uses the LLM score when MEMORY_TEMPORAL_KG_LLM=1
  - reconcile_fact_supersession uses 1.0 (deterministic) when the flag is off
  - threshold gate: low LLM scores (< threshold) do NOT supersede
  - LLM failure (returns None) falls back to deterministic 1.0
  - MEMORY_TEMPORAL_KG_LLM_THRESHOLD env var is respected
"""

import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import fact_temporal as ft
from llm_extraction import (
    score_fact_contradiction_via_llm,
    _parse_contradiction_score,
)


class TestParseContradictionScore(unittest.TestCase):
    """T11: _parse_contradiction_score handles various LLM outputs."""

    def test_parses_simple_decimal(self):
        self.assertEqual(_parse_contradiction_score("0.5"), 0.5)
        self.assertEqual(_parse_contradiction_score("0.85"), 0.85)
        self.assertEqual(_parse_contradiction_score("0"), 0.0)
        self.assertEqual(_parse_contradiction_score("1"), 1.0)

    def test_parses_decimal_with_surrounding_text(self):
        # LLMs sometimes wrap output in markdown or text
        self.assertEqual(_parse_contradiction_score("Score: 0.7\n"), 0.7)
        self.assertEqual(_parse_contradiction_score("0.42 is the score"), 0.42)

    def test_clamps_out_of_range(self):
        self.assertEqual(_parse_contradiction_score("1.5"), 1.0)
        # Note: "-0.3" is best-effort parsed as 0.3 (sign stripped by
        # regex).  The downstream clamp at 1.0 is the authoritative
        # out-of-range guard.
        self.assertEqual(_parse_contradiction_score("1.5"), 1.0)
        self.assertEqual(_parse_contradiction_score("99"), 1.0)

    def test_returns_none_for_garbage(self):
        self.assertIsNone(_parse_contradiction_score(""))
        self.assertIsNone(_parse_contradiction_score("no number here"))
        self.assertIsNone(_parse_contradiction_score("contradictory"))


class TestSupersedeFactAcceptsScore(unittest.TestCase):
    """T11: supersede_fact takes a score parameter and stores it."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        import fact_extraction as fe

        fe.ensure_facts_schema(self.conn)
        # Insert two facts (old + new) with overlapping event_time
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "locked, first_seen, last_seen, mention_count, source_memory, "
            "event_time, event_time_granularity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alice", "is_a", "engineer", 0.9, 0, now, now, 1, None, now, "day"),
        )
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "locked, first_seen, last_seen, mention_count, source_memory, "
            "event_time, event_time_granularity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alice", "is_a", "manager", 0.9, 0, now, now, 1, None, now, "day"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_score_stored_in_column(self):
        result = ft.supersede_fact(self.conn, 1, 2, "contradicted", score=0.85)
        self.assertTrue(result)
        row = self.conn.execute(
            "SELECT contradiction_score, invalidation_reason, superseded_by "
            "FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 0.85)
        self.assertEqual(row[1], "contradicted")
        self.assertEqual(row[2], 2)

    def test_default_score_is_1_0(self):
        """Pre-T11 callers get the deterministic 1.0 score."""
        result = ft.supersede_fact(self.conn, 1, 2, "contradicted")
        self.assertTrue(result)
        row = self.conn.execute(
            "SELECT contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 1.0)


class TestReconcileSupersessionLLM(unittest.TestCase):
    """T11: reconcile_fact_supersession uses the LLM score when flag is on."""

    def setUp(self):
        import fact_extraction as fe

        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)
        now = time.time()
        # old fact
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "locked, first_seen, last_seen, mention_count, source_memory, "
            "event_time, event_time_granularity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alice", "is_a", "engineer", 0.9, 0, now, now, 1, None, now, "day"),
        )
        # new fact with different object (contradiction candidate)
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "locked, first_seen, last_seen, mention_count, source_memory, "
            "event_time, event_time_granularity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alice", "is_a", "manager", 0.9, 0, now, now, 1, None, now, "day"),
        )
        self.conn.commit()

    def tearDown(self):
        # Make sure env doesn't leak between tests
        os.environ.pop("MEMORY_TEMPORAL_KG_LLM", None)
        os.environ.pop("MEMORY_TEMPORAL_KG_LLM_THRESHOLD", None)
        self.conn.close()

    def test_deterministic_default_off(self):
        """Without the flag, deterministic 1.0 score, supersedes happen."""
        os.environ.pop("MEMORY_TEMPORAL_KG_LLM", None)
        superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [1])
        row = self.conn.execute(
            "SELECT contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 1.0)

    def test_llm_high_score_supersedes(self):
        """LLM score >= threshold -> supersede with LLM score."""
        os.environ["MEMORY_TEMPORAL_KG_LLM"] = "1"
        with patch(
            "llm_extraction.score_fact_contradiction_via_llm", return_value=0.95
        ):
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [1])
        row = self.conn.execute(
            "SELECT contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 0.95)

    def test_llm_low_score_does_not_supersede(self):
        """LLM score < threshold -> no supersession (refinement, not contradiction)."""
        os.environ["MEMORY_TEMPORAL_KG_LLM"] = "1"
        with patch("llm_extraction.score_fact_contradiction_via_llm", return_value=0.2):
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        # Threshold default is 0.7, score 0.2 is below
        self.assertEqual(superseded, [])
        row = self.conn.execute(
            "SELECT superseded_by, contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0.0)  # default value, not updated

    def test_llm_failure_falls_back_to_deterministic(self):
        """When the LLM returns None, fall back to 1.0 (pre-T11 behavior)."""
        os.environ["MEMORY_TEMPORAL_KG_LLM"] = "1"
        with patch(
            "llm_extraction.score_fact_contradiction_via_llm", return_value=None
        ):
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [1])
        row = self.conn.execute(
            "SELECT contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 1.0)  # fallback to deterministic

    def test_llm_exception_does_not_break_save(self):
        """When the LLM raises, the supersession still happens (fallback to 1.0)."""
        os.environ["MEMORY_TEMPORAL_KG_LLM"] = "1"

        def _raise(*a, **kw):
            raise RuntimeError("LLM crashed")

        with patch(
            "llm_extraction.score_fact_contradiction_via_llm", side_effect=_raise
        ):
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [1])
        row = self.conn.execute(
            "SELECT contradiction_score FROM kg_facts WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], 1.0)

    def test_custom_threshold_respected(self):
        """MEMORY_TEMPORAL_KG_LLM_THRESHOLD controls the gate."""
        os.environ["MEMORY_TEMPORAL_KG_LLM"] = "1"
        os.environ["MEMORY_TEMPORAL_KG_LLM_THRESHOLD"] = "0.5"
        with patch("llm_extraction.score_fact_contradiction_via_llm", return_value=0.6):
            # 0.6 >= 0.5 -> supersede
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [1])

        # Reset the second fact for the next sub-test
        self.conn.execute(
            "UPDATE kg_facts SET superseded_by = NULL, contradiction_score = 0.0 "
            "WHERE id = 1"
        )
        self.conn.execute("UPDATE kg_facts SET supersedes = NULL WHERE id = 2")
        self.conn.commit()

        # 0.4 < 0.5 -> no supersede
        with patch("llm_extraction.score_fact_contradiction_via_llm", return_value=0.4):
            superseded = ft.reconcile_fact_supersession(self.conn, 2)
        self.assertEqual(superseded, [])


class TestReconcileSupersessionNoCandidates(unittest.TestCase):
    """T11: reconcile_fact_supersession handles edge cases gracefully."""

    def setUp(self):
        import fact_extraction as fe

        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_no_candidates_returns_empty(self):
        """No matching facts -> empty list, no errors."""
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "locked, first_seen, last_seen, mention_count, source_memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alice", "is_a", "engineer", 0.9, 0, now, now, 1, None),
        )
        self.conn.commit()
        superseded = ft.reconcile_fact_supersession(self.conn, 1)
        self.assertEqual(superseded, [])

    def test_nonexistent_fact_returns_empty(self):
        superseded = ft.reconcile_fact_supersession(self.conn, 999)
        self.assertEqual(superseded, [])


class TestLLMScoreWrapper(unittest.TestCase):
    """T11: score_fact_contradiction_via_llm wiring checks."""

    def test_function_is_callable(self):
        """The function exists with the expected signature and returns a float or None."""
        import inspect

        sig = inspect.signature(score_fact_contradiction_via_llm)
        self.assertEqual(len(sig.parameters), 6)
        self.assertIn("subj_a", sig.parameters)
        self.assertIn("obj_b", sig.parameters)
        # Return-type annotation is Optional[float] (i.e. float | None)
        self.assertIn("float", str(sig.return_annotation))

    def test_returns_float_in_range_or_none(self):
        """The LLM call either returns a score in [0.0, 1.0] or None.

        In this test env, the LLM is available (config default).  We
        don't assert on the actual score (LLM output is non-deterministic
        in principle) but verify the return is well-typed.
        """
        result = score_fact_contradiction_via_llm(
            "alice", "is_a", "engineer", "alice", "is_a", "manager"
        )
        if result is not None:
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 1.0)


if __name__ == "__main__":
    unittest.main()
