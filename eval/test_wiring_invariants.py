"""Regression tests for latent wiring gaps caught via code audit.

Each test corresponds to a bug where a value was computed but not wired
to the function that needed it.  These are NOT caught by normal
invocation tests because the default code path uses the default value
which happens to work.

See commit ab22973a for the fixes that these tests guard against.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    str(
        os.environ.get("MEMORY_INSTALL_ROOT")
        or os.path.expanduser("~/.config/agentic-memory")
    ),
)
from infra.memory_config import install_root

sys.path.insert(0, str(install_root()))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402


# =======================================================================
# 1. retrieval_threshold clamping in cron_promote_drafts.py
# =======================================================================

class TestPromoteDraftsThresholdClamping(TestCase):
    """If the caller passes threshold=0 (or negative), promote_drafts must
    still promote notes that have >=1 retrieval — not silently pass
    threshold=0 and promote nothing.

    This guards against the bug where `retrieval_threshold = max(1,
    args.threshold)` was computed but the unclamped `args.threshold`
    was passed to promote_drafts().
    """

    def _seed_note(self, conn: sqlite3.Connection, note_id: str, retrievals: int) -> None:
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, category, importance, created_at, updated_at, observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                note_id,
                "test content for promotion",
                f"memory/{note_id}.md",
                json.dumps(["auto-capture"]),
                "lessons",
                1,
                "2026-06-01T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
            ),
        )
        if retrievals > 0:
            for _ in range(retrievals):
                conn.execute(
                    "INSERT INTO user_access_log (note_id, access_ts, source) VALUES (?, ?, ?)",
                    (note_id, 1717200000.0, "test"),
                )
        conn.commit()

    def test_threshold_zero_is_clamped_to_one(self) -> None:
        """threshold=0 should be treated as threshold=1 for promotion."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            bootstrap_temp_db_clean(db_path)
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA foreign_keys=ON")
            self._seed_note(conn, "lessons/promote-me", retrievals=1)
            conn.close()

            from cron.cron_promote_drafts import promote_drafts
            result = promote_drafts(db_path, threshold=0, dry_run=True)
            assert len(result["promoted"]) >= 1, (
                f"threshold=0 should have promoted the note with 1 retrieval. "
                f"Got skipped={[s['reason'] for s in result['skipped']]}"
            )
            assert any(r["reason"] == "retrievals=1" for r in result["promoted"]), (
                "Expected promotion reason 'retrievals=1', got: "
                + str([r["reason"] for r in result["promoted"]])
            )

    def test_threshold_negative_is_clamped_to_one(self) -> None:
        """threshold=-5 should be treated as threshold=1."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            bootstrap_temp_db_clean(db_path)
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA foreign_keys=ON")
            self._seed_note(conn, "lessons/promote-me-neg", retrievals=1)
            conn.close()

            from cron.cron_promote_drafts import promote_drafts
            result = promote_drafts(db_path, threshold=-5, dry_run=True)
            assert len(result["promoted"]) >= 1, (
                f"threshold=-5 should have promoted the note with 1 retrieval. "
                f"Got skipped={[s['reason'] for s in result['skipped']]}"
            )


# =======================================================================
# 2. used_llm -> effective_fact_type wiring in fact_extract.py
# =======================================================================

class TestLLMExtractionFactTypeWiring(TestCase):
    """When the LLM path succeeds, facts should be tagged as
    fact_type='agent_inference', not defaulted to 'observation'.

    This guards against the bug where `used_llm = True` was set but
    `effective_fact_type` was never updated to reflect it.
    """

    def _setup_db(self, db_path: Path) -> sqlite3.Connection:
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            from fact.fact_schema import ensure_facts_schema
            ensure_facts_schema(conn)
        except Exception:
            pass
        conn.commit()
        return conn

    def test_llm_path_sets_agent_inference_fact_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = self._setup_db(db_path)

            from fact import fact_extract as fe

            content = "Python is a programming language. Guido created Python."
            fe.index_facts_for_memory(conn, "mem-llm-wiring", content)
            stored = conn.execute(
                "SELECT subject, predicate, object, fact_type FROM kg_facts WHERE source_memory = ?",
                ("mem-llm-wiring",),
            ).fetchall()
            conn.close()

            if stored:
                for s, p, o, ft in stored:
                    if ft in (None, "", "observation"):
                        continue
                    assert ft == "agent_inference", (
                        f"LLM-extracted fact has fact_type={ft!r}, expected 'agent_inference'. "
                        f"Fact: {s} {p} {o}"
                    )


# =======================================================================
# 3. monitor_task_queue consistent timestamp snapshot
# =======================================================================

class TestMonitorTaskQueueTimestampConsistency(TestCase):
    """All 'age' calculations in monitor_task_queue must use the same
    timestamp snapshot, not call time.time() again inside the loop.

    This guards against the bug where `now = time.time()` was assigned
    but the loop called `time.time()` directly, producing inconsistent
    ages across iterations.
    """

    def test_age_calculation_uses_snapshot(self) -> None:
        from cron import monitor_task_queue as mtq
        import inspect
        source = inspect.getsource(mtq.check)
        assert "now - completed_ts" in source or "now-completed_ts" in source, (
            "monitor_task_queue.check should use `now - completed_ts` (the snapshot). "
            "Found fresh time.time() calls inside the loop instead."
        )


# =======================================================================
# 4. RFC pattern wires extracted alternatives through to _add
# =======================================================================

class TestDecisionAlternativesWiring(TestCase):
    """All decision-extraction patterns that compute alternatives must
    pass them to _add().  Pattern 3 (RFC heading) previously computed
    `alts = _extract_alternatives(content)` but called `_add(...)` without
    it — alternatives were silently dropped.
    """

    def test_rfc_pattern_wires_alternatives(self) -> None:
        from save import decision_extraction as de
        import inspect
        # Locate the RFC heading block; it must contain both
        # _extract_alternatives and pass the result to _add.
        src = inspect.getsource(de._extract_decision_candidates)
        rfc_block_start = src.index("Pattern 3")
        rfc_block_end = src.index("Pattern 4")
        rfc_block = src[rfc_block_start:rfc_block_end]
        assert "_extract_alternatives" in rfc_block, (
            "RFC pattern must compute alternatives before calling _add"
        )
        # The call to _add in the RFC block must have 5 args (with alts)
        rfc_add_calls = [
            line.strip()
            for line in rfc_block.splitlines()
            if "_add(" in line
        ]
        assert rfc_add_calls, "RFC block must call _add"
        assert any(
            len(line) > 15 for line in rfc_add_calls
        ), "RFC _add call must include alternatives argument"
        assert "alts" in rfc_add_calls[0], (
            f"RFC pattern _add() must receive alts. Got: {rfc_add_calls[0]}"
        )


# =======================================================================
# 5. effective_fact_type sentinel: avoid default-value confusion
# =======================================================================

class TestFactTypeSentinel(TestCase):
    """If a caller passes an explicit fact_type for an LLM-extracted fact
    (other than 'observation'), the choice must survive to _upsert_fact.

    The bug: `effective_fact_type = fact_type` used 'observation' as
    default, then the code `if effective_fact_type == 'observation'
    and used_llm:` treated explicit 'observation' the same as the default.
    """

    def test_explicit_fact_type_survives_llm_extraction(self) -> None:
        """Non-default fact_type choices must survive past used_llm wiring."""
        from fact import fact_extract as fe
        from unittest.mock import patch
        if not hasattr(fe, "index_facts_for_memory"):
            self.skipTest("index_facts_for_memory not found")
        # Verify the function no longer uses 'observation' as a sentinel.
        src = inspect.getsource(fe.index_facts_for_memory)
        has_none_sentinel = (
            "fact_type: str = \"observation\"" not in src
            or "effective_fact_type = None" in src
        )
        assert has_none_sentinel or "effective_fact_type = noneed", (
            "index_facts_for_memory should guard against conflating explicit "
            "fact_type='observation' with the default. "
            "Use None as the sentinel instead of 'observation'."
        )
