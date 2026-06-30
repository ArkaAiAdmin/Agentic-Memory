"""Tests for Phase 9: Summarization, User Profile, Adaptive Retention, Multi-Agent."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reset lazy-config caches BEFORE any module-level ``from X import LAZY_CONST``
# statements fire.  make_lazy_getattr caches resolved values in the target
# module's __dict__; a prior test that triggered the getter (e.g. a
# TestUserProfileIntegration setUp that sets MEMORY_USER_PROFILE=1) will leave
# ``PROFILE_ENABLED=True`` permanently baked in for the rest of the process
# unless we clear it here.
from infra.memory_common import reset_all_lazy_config_attrs

reset_all_lazy_config_attrs()

from summarization import (
    _split_sentences,
    _tokenize_words,
    _compute_tfidf,
    summarize_text,
)
from user_profile import (
    _decay_weight,
    record_access,
    get_user_profile,
    personalize_results,
)
from adaptive_retention import (
    compute_adaptive_halflife,
    ADAPTIVE_RETENTION_ENABLED,
)
from memory_sharing import (
    _ensure_shared_table,
    share_memory,
    list_shared_memories,
    import_shared_memory,
)


class TestSplitSentences(unittest.TestCase):
    def test_basic_split(self):
        sents = _split_sentences(
            "Hello world. How are you today? I am doing fine here."
        )
        self.assertEqual(len(sents), 3)

    def test_short_text(self):
        sents = _split_sentences("Hello world")
        self.assertEqual(len(sents), 1)


class TestTokenizeWords(unittest.TestCase):
    def test_removes_stop_words(self):
        tokens = _tokenize_words("The quick brown fox is here")
        self.assertNotIn("the", tokens)
        self.assertNotIn("is", tokens)
        self.assertIn("quick", tokens)
        self.assertIn("brown", tokens)
        self.assertIn("fox", tokens)

    def test_empty(self):
        tokens = _tokenize_words("")
        self.assertEqual(tokens, [])


class TestComputeTfidf(unittest.TestCase):
    def test_scores_sentences(self):
        sents = [
            "The quick brown fox jumps over the lazy dog",
            "Python is a popular programming language for AI",
            "The fox runs quickly through the forest",
        ]
        scores = _compute_tfidf(sents)
        self.assertEqual(len(scores), 3)
        for s in scores:
            self.assertIn("score", s)
            self.assertIn("sentence", s)


class TestSummarizeText(unittest.TestCase):
    def test_short_text_returned_as_is(self):
        text = "Short text."
        result = summarize_text(text)
        self.assertEqual(result, text)

    def test_empty_text(self):
        result = summarize_text("")
        self.assertEqual(result, "")

    def test_long_text_summarized(self):
        text = (
            "Python is a versatile programming language used in many domains. "
            "It has a simple syntax that makes it easy to learn. "
            "Machine learning is one of the most popular applications of Python today. "
            "Deep learning frameworks like TensorFlow and PyTorch are written in Python. "
            "Data science teams use Python for analysis and visualization of large datasets. "
            "Short. "
            "Python standard library provides tools for many common tasks and operations. "
            "The community around Python is large and active with thousands of contributors. "
        )
        result = summarize_text(text, max_sentences=3)
        # Summary should have fewer sentences than original
        from summarization import _split_sentences

        self.assertLess(len(_split_sentences(result)), len(_split_sentences(text)))


class TestUserProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import user_profile

        cls._original_enabled = user_profile.__dict__.get("PROFILE_ENABLED")
        user_profile.__dict__.pop("PROFILE_ENABLED", None)
        user_profile.PROFILE_ENABLED = False  # bypass lazy getter

    @classmethod
    def tearDownClass(cls):
        import user_profile as _up
        _up.__dict__.pop("PROFILE_ENABLED", None)
        if cls._original_enabled is not None:
            _up.__dict__["PROFILE_ENABLED"] = cls._original_enabled

    def test_decay_weight_recent(self):
        w = _decay_weight(0)
        self.assertAlmostEqual(w, 1.0, places=2)

    def test_decay_weight_old(self):
        w = _decay_weight(90)
        self.assertLess(w, 1.0)

    def test_record_access_disabled(self):
        result = record_access("test:note1", "search")
        import user_profile as _up
        self.assertFalse(_up.PROFILE_ENABLED)
        self.assertFalse(result)


class TestPersonalizeResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import user_profile

        cls._original_enabled = user_profile.__dict__.get("PROFILE_ENABLED")
        user_profile.__dict__.pop("PROFILE_ENABLED", None)
        user_profile.PROFILE_ENABLED = False  # bypass lazy getter

    @classmethod
    def tearDownClass(cls):
        import user_profile as _up
        _up.__dict__.pop("PROFILE_ENABLED", None)
        if cls._original_enabled is not None:
            _up.__dict__["PROFILE_ENABLED"] = cls._original_enabled

    def test_empty_results(self):
        result = personalize_results([])
        self.assertEqual(result, [])

    def test_boost_matching_category(self):
        import user_profile as _up
        _up.__dict__.pop("PROFILE_ENABLED", None)
        _up.PROFILE_ENABLED = False  # ensure disabled to use ad-hoc profile arg
        results = [
            {"content": "A", "category": "lessons", "score": 1.0},
            {"content": "B", "category": "other", "score": 1.0},
        ]
        profile = {
            "enabled": True,
            "top_categories": [("lessons", 5.0), ("other", 1.0)],
            "top_tags": [],
        }
        boosted = personalize_results(results, profile=profile, boost_factor=2.0)
        self.assertGreaterEqual(boosted[0]["score"], boosted[1]["score"])


class TestAdaptiveRetention(unittest.TestCase):
    def test_disabled_returns_default(self):
        hl = compute_adaptive_halflife("test:note", 180.0)
        if not ADAPTIVE_RETENTION_ENABLED:
            self.assertEqual(hl, 180.0)

    def test_halflife_bounds(self):
        if ADAPTIVE_RETENTION_ENABLED:
            hl = compute_adaptive_halflife("test:note", 180.0)
            self.assertGreaterEqual(hl, 30)
            self.assertLessEqual(hl, 730)


class TestMultiAgent(unittest.TestCase):
    def test_ensure_shared_table(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            conn = sqlite3.connect(db)
            _ensure_shared_table(conn)
            # Table should exist
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            self.assertIn("shared_memories", table_names)
            conn.close()
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# Integration tests: DB-dependent MCP tool paths
# ---------------------------------------------------------------------------


class TestUserProfileIntegration(unittest.TestCase):
    """Integration tests for user_profile with a real DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                source TEXT DEFAULT 'search',
                category TEXT,
                tags TEXT,
                accessed_at REAL NOT NULL
            )
        """)
        self.conn.commit()
        self.conn.close()
        # Enable feature for testing and reload module
        os.environ["MEMORY_USER_PROFILE"] = "1"
        import user_profile

        importlib.reload(user_profile)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "MEMORY_USER_PROFILE" in os.environ:
            del os.environ["MEMORY_USER_PROFILE"]

    def test_record_and_retrieve_access(self):
        """Test that recording access and retrieving profile works end-to-end."""
        record_access("test:note1", "search", db_path=self.db_path)
        record_access("test:note2", "search", db_path=self.db_path)
        record_access("test:note3", "list", db_path=self.db_path)

        profile = get_user_profile(db_path=self.db_path)

        self.assertIn("enabled", profile)
        self.assertTrue(profile["enabled"])
        self.assertIn("top_categories", profile)
        self.assertIn("top_tags", profile)
        self.assertIsInstance(profile["top_categories"], list)
        self.assertIsInstance(profile["top_tags"], list)


class TestMultiAgentIntegration(unittest.TestCase):
    """Integration tests for multi_agent with a real DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                valid_from TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT,
                category TEXT,
                tier TEXT DEFAULT 'warm',
                metadata TEXT,
                repo_id TEXT,
                hash TEXT
            )
        """)
        _ensure_shared_table(self.conn)
        self.conn.commit()
        self.conn.close()
        # Enable feature for testing and reload module
        os.environ["MEMORY_MULTI_AGENT"] = "1"
        import memory_sharing

        importlib.reload(memory_sharing)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up env
        if "MEMORY_MULTI_AGENT" in os.environ:
            del os.environ["MEMORY_MULTI_AGENT"]

    def test_share_and_list_memory(self):
        """Test that sharing a memory and listing it works end-to-end."""
        # Insert a note into the memories table first
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "test:shared1",
                "Shared content here",
                "test",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
            ),
        )
        self.conn.commit()
        self.conn.close()

        # Share a memory
        result = share_memory("test:shared1", "agent_alpha", db_path=self.db_path)
        self.assertTrue(result.get("enabled", False))
        self.assertNotIn("error", result)

        # List shared memories
        memories = list_shared_memories(db_path=self.db_path)
        self.assertIsInstance(memories, list)
        # Should contain our shared memory
        note_ids = [m.get("source_note_id") for m in memories]
        self.assertIn("test:shared1", note_ids)

    def test_import_shared_memory(self):
        """Test importing a shared memory from another agent."""
        # Insert a note into the memories table first
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "test:import1",
                "Import content here",
                "test",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
            ),
        )
        self.conn.commit()
        self.conn.close()

        # First share a memory
        share_res = share_memory("test:import1", "agent_beta", db_path=self.db_path)
        shared_id = share_res["shared_id"]

        # Import it
        result = import_shared_memory(shared_id, "agent_gamma", db_path=self.db_path)
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)


class TestSummarizeIntegration(unittest.TestCase):
    """Integration tests for summarization with a real DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT
            )
        """)
        self.conn.commit()
        self.conn.close()
        # Enable feature for testing and reload module
        os.environ["MEMORY_SUMMARIZATION"] = "1"
        import summarization

        importlib.reload(summarization)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up env
        if "MEMORY_SUMMARIZATION" in os.environ:
            del os.environ["MEMORY_SUMMARIZATION"]

    def test_summarize_existing_note(self):
        """Test summarizing an existing note in the DB."""
        # Insert a long note
        long_content = (
            "Python is a versatile programming language used in many domains. "
            "It has a simple syntax that makes it easy to learn. "
            "Machine learning is one of the most popular applications of Python today. "
            "Deep learning frameworks like TensorFlow and PyTorch are written in Python. "
            "Data science teams use Python for analysis and visualization of large datasets. "
        ) * 3  # Make it long enough to trigger summarization

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "test:long_note",
                long_content,
                "test",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
        conn.close()

        # Import the MCP tool function
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from memory_mcp import memory_summarize

        # Call the tool
        result = memory_summarize("test:long_note")
        self.assertIsInstance(result, str)
        # Should return a summary (not the full content)
        self.assertLess(len(result), len(long_content))


class TestAdaptiveRetentionIntegration(unittest.TestCase):
    """Integration tests for adaptive_retention with a real DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT
            )
        """)
        self.conn.commit()
        self.conn.close()
        # Enable feature for testing and reload module
        self._old_adaptive = os.environ.get("MEMORY_ADAPTIVE_RETENTION")
        os.environ["MEMORY_ADAPTIVE_RETENTION"] = "1"
        import adaptive_retention

        importlib.reload(adaptive_retention)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up env
        if self._old_adaptive is not None:
            os.environ["MEMORY_ADAPTIVE_RETENTION"] = self._old_adaptive
        else:
            os.environ.pop("MEMORY_ADAPTIVE_RETENTION", None)

    def test_compute_halflife(self):
        """Test computing adaptive halflife via batch operation."""
        # Insert a note with some access history
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, access_count, last_accessed, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test:note",
                "Test content",
                "test",
                10,
                "2026-06-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
        conn.close()

        # Import the MCP tool function
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from memory_mcp import memory_adaptive_retention

        # Call the tool (batch mode, dry_run=True to avoid writes)
        result = memory_adaptive_retention(dry_run=True)
        self.assertIsInstance(result, str)
        # Should return JSON with status
        data = json.loads(result)
        self.assertIn("adaptive_retention", data)


if __name__ == "__main__":
    unittest.main()
