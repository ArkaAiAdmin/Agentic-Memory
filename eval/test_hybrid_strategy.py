"""Tests for the P3.3 hybrid strategy (regex default, LLM for high-value).

Covers:
- _should_use_llm_for_memory returns False when LLM unavailable
- _should_use_llm_for_memory returns True when memory is pinned
- _should_use_llm_for_memory returns True when importance_score >= threshold
- _should_use_llm_for_memory returns False when importance_score < threshold
- _should_use_llm_for_memory returns False when memory not found
- MEMORY_LLM_FORCE=1 overrides the threshold
- MEMORY_LLM_HYBRID=0 disables LLM entirely
- MEMORY_LLM_HYBRID_THRESHOLD env var overrides config default
- index_facts_for_memory uses regex when not high-value
- index_facts_for_memory tries LLM when high-value
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_module(name: str):
    """Load a module fresh from REPO."""
    spec = importlib.util.spec_from_file_location(
        f"_test_{name}", str(REPO / f"{name}.py")
    )
    if spec is None:
        raise RuntimeError(f"Could not load {name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_test_{name}"] = mod
    loader = spec.loader
    if loader is None:
        raise RuntimeError(f"spec.loader is None for {name}.py")
    loader.exec_module(mod)
    return mod


class TestShouldUseLlmForMemory(unittest.TestCase):
    """_should_use_llm_for_memory respects pinned, importance, env vars."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                pinned INTEGER DEFAULT 0,
                importance_score REAL,
                deleted_at TEXT
            );
            CREATE TABLE kg_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                confidence REAL,
                source_memory TEXT,
                mention_count INTEGER DEFAULT 1,
                first_seen REAL,
                last_seen REAL
            );
            """
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("regular", "regular content", 0, 0.3),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("pinned", "pinned content", 1, 0.1),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("high_score", "high score content", 0, 0.8),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("no_score", "no score content", 0, None),
        )
        self.conn.commit()

        self._saved_env = {}
        for k in (
            "MEMORY_LLM_HYBRID_THRESHOLD",
            "MEMORY_LLM_FORCE",
            "MEMORY_LLM_HYBRID",
        ):
            self._saved_env[k] = os.environ.get(k)
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pinned_memory_uses_llm(self):
        """Pinned memories always use LLM regardless of importance."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertTrue(fe._should_use_llm_for_memory(self.conn, "pinned"))

    def test_high_importance_uses_llm(self):
        """importance_score >= threshold (0.5) uses LLM."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertTrue(fe._should_use_llm_for_memory(self.conn, "high_score"))

    def test_low_importance_skips_llm(self):
        """importance_score < threshold uses regex only."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "regular"))

    def test_null_importance_skips_llm(self):
        """NULL importance_score skips LLM (unless pinned)."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "no_score"))

    def test_memory_not_found_skips_llm(self):
        """Unknown memory id skips LLM (defensive default)."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "doesnt_exist"))

    def test_llm_unavailable_skips_llm(self):
        """When LLM is unavailable, returns False (regex only)."""
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=False):
            # Even pinned memory should return False
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "pinned"))

    def test_force_flag_overrides(self):
        """MEMORY_LLM_FORCE=1 returns True for all memories."""
        os.environ["MEMORY_LLM_FORCE"] = "1"
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertTrue(fe._should_use_llm_for_memory(self.conn, "regular"))
            self.assertTrue(fe._should_use_llm_for_memory(self.conn, "pinned"))

    def test_disable_flag_overrides(self):
        """MEMORY_LLM_HYBRID=0 returns False even for high-value."""
        os.environ["MEMORY_LLM_HYBRID"] = "0"
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "pinned"))
            self.assertFalse(fe._should_use_llm_for_memory(self.conn, "high_score"))

    def test_env_threshold_overrides(self):
        """MEMORY_LLM_HYBRID_THRESHOLD env var changes the cutoff."""
        os.environ["MEMORY_LLM_HYBRID_THRESHOLD"] = "0.2"
        fe = _load_module("fact_extraction")
        with patch("llm_extraction.is_llm_extraction_available", return_value=True):
            # Regular memory (0.3) now exceeds 0.2 threshold
            self.assertTrue(fe._should_use_llm_for_memory(self.conn, "regular"))


class TestIndexFactsForMemoryUsesRegexByDefault(unittest.TestCase):
    """When the memory is low-value, the regex path is used and LLM is skipped."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                pinned INTEGER DEFAULT 0,
                importance_score REAL,
                deleted_at TEXT
            );
            CREATE TABLE kg_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                confidence REAL,
                source_memory TEXT,
                mention_count INTEGER DEFAULT 1,
                first_seen REAL,
                last_seen REAL,
                locked INTEGER DEFAULT 0,
                context TEXT,
                subject_entity_id INTEGER,
                object_entity_id INTEGER,
                event_time REAL,
                event_time_granularity TEXT,
                transaction_time REAL,
                valid_at REAL,
                invalid_at REAL,
                superseded_by INTEGER,
                supersedes INTEGER,
                contradiction_score REAL DEFAULT 0.0,
                invalidation_reason TEXT
            );
            """
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            (
                "low_value",
                "Some markdown content with **bold label:** description text",
                0,
                0.1,
            ),
        )
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("high_value", "Some content for high value test", 1, 0.1),
        )
        self.conn.commit()

        self._saved_env = {}
        for k in ("MEMORY_LLM_FORCE", "MEMORY_LLM_HYBRID"):
            self._saved_env[k] = os.environ.get(k)
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_low_value_uses_regex(self):
        """When memory is low-value, no LLM call is made; regex is used."""
        fe = _load_module("fact_extraction")
        called = {"llm": 0}

        def fake_llm_extract(content):
            called["llm"] += 1
            return []

        with (
            patch("llm_extraction.is_llm_extraction_available", return_value=True),
            patch("llm_extraction.extract_facts_via_llm", side_effect=fake_llm_extract),
        ):
            result = fe.index_facts_for_memory(
                self.conn,
                "low_value",
                "Some markdown content with **bold label:** description text",
            )
            # LLM was not called because memory is low-value
            self.assertEqual(called["llm"], 0)
            self.assertIn("facts", result)

    def test_high_value_calls_llm(self):
        """When memory is high-value (pinned), LLM is called first."""
        fe = _load_module("fact_extraction")
        called = {"llm": 0}

        def fake_llm_extract(content):
            called["llm"] += 1
            return [("fake", "is_a", "subject", 0.9)]

        with (
            patch("llm_extraction.is_llm_extraction_available", return_value=True),
            patch("llm_extraction.extract_facts_via_llm", side_effect=fake_llm_extract),
        ):
            fe.index_facts_for_memory(self.conn, "high_value", "Some content")
            # LLM was called
            self.assertEqual(called["llm"], 1)
            # And the fact was inserted
            row = self.conn.execute(
                "SELECT subject, predicate, object FROM kg_facts "
                "WHERE source_memory = 'high_value'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "fake")


class TestBackfillCliHybridFlags(unittest.TestCase):
    """backfill_all.py --llm-hybrid-threshold and --no-llm-hybrid flags."""

    def _run_driver(self, argv, env_var):
        driver = REPO / "memory" / f".test_hybrid_driver_{env_var}.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import backfill_all as ba\n"
            "ba.backfill_all = lambda *a, **kw: {'operations': [], 'result': 'stub'}\n"
            f"sys.argv = {argv!r}\n"
            "import os\n"
            "ba.main()\n"
            f"got = os.environ.get({env_var!r})\n"
            f"assert got is not None, 'env var not set'\n"
            "print('OK')\n"
        )
        try:
            import subprocess

            result = subprocess.run(
                [sys.executable, str(driver)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            try:
                driver.unlink()
            except Exception:
                pass
        return result

    def test_no_llm_hybrid_sets_env(self):
        """--no-llm-hybrid sets MEMORY_LLM_HYBRID=0."""
        result = self._run_driver(
            ["backfill_all", "--no-llm-hybrid"], "MEMORY_LLM_HYBRID"
        )
        self.assertEqual(
            result.returncode,
            0,
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_llm_force_sets_env(self):
        """--llm-force sets MEMORY_LLM_FORCE=1."""
        result = self._run_driver(["backfill_all", "--llm-force"], "MEMORY_LLM_FORCE")
        self.assertEqual(
            result.returncode,
            0,
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_llm_hybrid_threshold_sets_env(self):
        """--llm-hybrid-threshold sets MEMORY_LLM_HYBRID_THRESHOLD."""
        result = self._run_driver(
            ["backfill_all", "--llm-hybrid-threshold", "0.25"],
            "MEMORY_LLM_HYBRID_THRESHOLD",
        )
        self.assertEqual(
            result.returncode,
            0,
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        self.assertIn("OK", result.stdout)


class TestBulkFunctionLLMSafe(unittest.TestCase):
    """The bulk function is structurally LLM-free.

    Regression for 2026-06-26 heartbeat hang: _backfill_drifted_subsystems
    iterated over 5,000+ memories and called index_facts_for_memory per
    row. With importance_score >= 0.5 on hundreds of notes, this loaded
    the 3B Qwen model onto MPS and tried to do inference per-memory,
    which deadlocked the loky worker pool and froze the machine for
    hours.

    The fix has two layers:
      1. The deny-by-exception flag (force_regex=True) was removed.
      2. The bulk path now uses a dedicated function —
         index_facts_for_memory_bulk — that physically cannot call LLM.
         The function body does not import llm_extraction, and tests
         assert that.

    These tests verify both layers. If anyone reverts the per-save
    function to use a force_regex flag, or moves the bulk call site
    back to the per-save function, the regression tests fail.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                pinned INTEGER DEFAULT 0,
                importance_score REAL,
                deleted_at TEXT
            );
            CREATE TABLE kg_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                confidence REAL,
                source_memory TEXT,
                mention_count INTEGER DEFAULT 1,
                first_seen REAL,
                last_seen REAL,
                locked INTEGER DEFAULT 0,
                context TEXT,
                subject_entity_id INTEGER,
                object_entity_id INTEGER,
                event_time REAL,
                event_time_granularity TEXT,
                transaction_time REAL,
                valid_at REAL,
                invalid_at REAL,
                superseded_by INTEGER,
                supersedes INTEGER,
                contradiction_score REAL DEFAULT 0.0,
                invalidation_reason TEXT
            );
            """
        )
        # Pinned + high-importance memory: would normally trigger LLM
        self.conn.execute(
            "INSERT INTO memories (id, content, pinned, importance_score) "
            "VALUES (?, ?, ?, ?)",
            ("pinned_note", "**Topic:** some content with entities", 1, 0.95),
        )
        self.conn.commit()

        self._saved_env = {}
        for k in ("MEMORY_LLM_FORCE", "MEMORY_LLM_HYBRID"):
            self._saved_env[k] = os.environ.get(k)
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bulk_function_exists(self):
        """index_facts_for_memory_bulk must exist as a separate function."""
        fe = _load_module("fact_extraction")
        self.assertTrue(
            hasattr(fe, "index_facts_for_memory_bulk"),
            "index_facts_for_memory_bulk must exist as a public function",
        )
        self.assertTrue(callable(fe.index_facts_for_memory_bulk))

    def test_per_save_function_no_force_regex_param(self):
        """The per-save index_facts_for_memory must NOT have a force_regex param.

        The deny-by-exception flag was the root design smell. If anyone
        re-adds it, this test fails — the fix must be the bulk function,
        not a flag on the per-save one.
        """
        import inspect

        fe = _load_module("fact_extraction")
        sig = inspect.signature(fe.index_facts_for_memory)
        self.assertNotIn(
            "force_regex",
            sig.parameters,
            "index_facts_for_memory must not accept force_regex — "
            "use index_facts_for_memory_bulk for bulk paths",
        )

    def test_bulk_function_source_does_not_reference_llm(self):
        """Source-inspect the bulk function: it must not import or call llm_extraction.

        This is the structural guarantee. The function body is regex
        only; the LLM call is physically unreachable from this path.
        """
        import inspect

        fe = _load_module("fact_extraction")
        source = inspect.getsource(fe.index_facts_for_memory_bulk)
        self.assertNotIn(
            "llm_extraction",
            source,
            "index_facts_for_memory_bulk must not reference llm_extraction",
        )
        self.assertNotIn(
            "extract_facts_via_llm",
            source,
            "index_facts_for_memory_bulk must not call extract_facts_via_llm",
        )

    def test_bulk_function_runtime_does_not_call_llm(self):
        """Run the bulk function and confirm LLM is not invoked at runtime."""
        fe = _load_module("fact_extraction")
        called = {"llm": 0}

        def fake_llm_extract(content):
            called["llm"] += 1
            return [("should", "not", "appear", 0.9)]

        with (
            patch("llm_extraction.is_llm_extraction_available", return_value=True),
            patch("llm_extraction.extract_facts_via_llm", side_effect=fake_llm_extract),
        ):
            result = fe.index_facts_for_memory_bulk(
                self.conn,
                "pinned_note",
                "**Topic:** some content with entities",
            )
        self.assertEqual(called["llm"], 0, "bulk function must not call LLM")
        self.assertIn("facts", result)

    def test_per_save_function_still_allows_llm(self):
        """Regression: per-save function must still allow LLM extraction."""
        fe = _load_module("fact_extraction")
        called = {"llm": 0}

        def fake_llm_extract(content):
            called["llm"] += 1
            return [("llm", "found", "this", 0.9)]

        with (
            patch("llm_extraction.is_llm_extraction_available", return_value=True),
            patch("llm_extraction.extract_facts_via_llm", side_effect=fake_llm_extract),
        ):
            fe.index_facts_for_memory(
                self.conn,
                "pinned_note",
                "**Topic:** some content with entities",
            )
        self.assertEqual(called["llm"], 1, "per-save function must allow LLM")

    def test_self_directed_backfill_uses_bulk_function(self):
        """Regression: self_directed._backfill_drifted_subsystems must call
        index_facts_for_memory_bulk (NOT the per-save variant) for the
        kg_facts branch. This is the exact path that caused the 2026-06-26
        hang. If anyone reverts this, the test fails.
        """
        import inspect
        import re

        from self_directed import _backfill_drifted_subsystems

        source = inspect.getsource(_backfill_drifted_subsystems)
        # The kg_facts backfill must use the bulk function
        self.assertIn(
            "index_facts_for_memory_bulk",
            source,
            "_backfill_drifted_subsystems must use "
            "index_facts_for_memory_bulk in the kg_facts backfill branch",
        )
        # The fact_extraction import block must include the bulk
        # function. Use a regex to handle multi-line parenthesised
        # imports correctly.
        match = re.search(r"from\s+fact_extraction\s+import\s+\(([^)]+)\)", source)
        self.assertIsNotNone(
            match,
            "self_directed must import from fact_extraction (parenthesised) "
            "for the backfill to work",
        )
        assert match is not None
        import_block = match.group(1)
        self.assertIn(
            "index_facts_for_memory_bulk",
            import_block,
            "The fact_extraction import block must include index_facts_for_memory_bulk",
        )
        # And the per-save function must NOT be in the import block —
        # self_directed's only need in the backfill is the bulk variant.
        # Use a word-boundary regex so "index_facts_for_memory_bulk"
        # doesn't match the substring "index_facts_for_memory".
        self.assertNotRegex(
            import_block,
            r"\bindex_facts_for_memory\b(?!_bulk)",
            "self_directed's fact_extraction import must NOT include the "
            "per-save index_facts_for_memory (would re-introduce the hang)",
        )


if __name__ == "__main__":
    unittest.main()
