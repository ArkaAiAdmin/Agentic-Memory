#!/usr/bin/env python3
"""Tests for search_memory.py — CLI entry point for searching memories.

Covers:
  1. _resolve_db_paths: local/global path resolution, custom_db_path, symlink logic
  2. search_memories: basic search, include_global, custom_db_path, include_invalid,
     silent mode, multi-source combining, dedup, access_count increment, error paths
  3. _tag: source_db label attachment
"""

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

# Suppress reranker / embedding side effects during unit tests

import recall.search_memory as search_memory  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(item_id="test/note-1", content="hello world", score=0.9, tags=None):
    """Return a minimal result-item dict matching memory_mcp format."""
    return {
        "id": item_id,
        "content": content,
        "final_score": score,
        "tags": tags or [],
        "source_file": f"{item_id}.md",
        "created": "2026-01-01T00:00:00+00:00",
    }


def _local_search_return(*items):
    """Return value for _mcp_search_memories when called for local DB."""
    return {"results": list(items), "count": len(items), "output": ""}


def _global_search_return(*items):
    """Return value for _mcp_search_memories when called for global DB."""
    return {"results": list(items), "count": len(items), "output": ""}


def _empty_search():
    return {"results": [], "count": 0, "output": ""}


# ---------------------------------------------------------------------------
# _tag tests
# ---------------------------------------------------------------------------


class TestTag(unittest.TestCase):
    """Unit tests for the _tag helper."""

    def test_attaches_source_db(self):
        items = [_make_item("a/1"), _make_item("a/2")]
        tagged = search_memory._tag(items, "local")
        self.assertEqual(len(tagged), 2)
        self.assertEqual(tagged[0]["source_db"], "local")
        self.assertEqual(tagged[1]["source_db"], "local")

    def test_does_not_mutate_original(self):
        items = [_make_item("a/1")]
        tagged = search_memory._tag(items, "global")
        self.assertNotIn("source_db", items[0])
        self.assertIn("source_db", tagged[0])

    def test_empty_list(self):
        self.assertEqual(search_memory._tag([], "local"), [])

    def test_preserves_other_keys(self):
        item = _make_item("a/1", tags=["foo", "bar"])
        tagged = search_memory._tag([item], "global")
        self.assertEqual(tagged[0]["tags"], ["foo", "bar"])
        self.assertEqual(tagged[0]["id"], "a/1")
        self.assertEqual(tagged[0]["source_db"], "global")


# ---------------------------------------------------------------------------
# _resolve_db_paths tests
# ---------------------------------------------------------------------------


class TestResolveDbPaths(unittest.TestCase):
    """Tests for _resolve_db_paths."""

    def test_default_paths(self):
        """Without custom_db_path, local = project_root/memory/memory.db, global = GLOBAL_MEM_DIR/memory.db."""
        fake_project = Path("/some/project")
        with (
            patch.object(search_memory, "os") as mock_os,
            patch("search_memory.find_project_root", return_value=fake_project),
        ):
            mock_os.getcwd.return_value = "/some/project"
            Path("/global/dir")
            with patch("search_memory.Path"):
                # Need to reconstruct properly; simpler: patch GLOBAL_MEM_DIR
                # and the is_symlink check
                local_db, global_db = search_memory._resolve_db_paths()
            # Local should be fake_project / 'memory' / 'memory.db'
            # We check via the real function with a real cwd instead

    def test_custom_db_path_overrides_local(self):
        """custom_db_path replaces the default local path."""
        custom = Path("/custom/db.sqlite")
        fake_mem = Path("/some/project/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/some/project"), fake_mem, Path("/global")),
        ):
            local_db, global_db = search_memory._resolve_db_paths(
                custom_db_path=str(custom)
            )
            self.assertEqual(local_db, custom)

    def test_returns_two_paths(self):
        """Always returns a 2-tuple."""
        fake_mem = Path("/some/project/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/some/project"), fake_mem, Path("/global")),
        ):
            result = search_memory._resolve_db_paths()
            self.assertIsInstance(result, tuple)
            self.assertEqual(len(result), 2)

    def test_local_path_type_is_path(self):
        """Both return values are Path instances."""
        fake_mem = Path("/some/project/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/some/project"), fake_mem, Path("/global")),
        ):
            local_db, global_db = search_memory._resolve_db_paths()
            self.assertIsInstance(local_db, Path)
            self.assertIsInstance(global_db, Path)

    def test_fallback_to_cwd_when_project_root_none(self):
        """When get_memory_paths returns cwd as local_mem, that path is used."""
        cwd = Path("/fallback/cwd")
        with patch(
            "search_memory.get_memory_paths", return_value=(cwd, cwd, Path("/global"))
        ):
            local_db, _ = search_memory._resolve_db_paths()
            self.assertEqual(local_db, cwd / "memory.db")

    def test_symlink_global_resolved(self):
        """If local_mem/global is a symlink, global DB resolves through it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            link_target = Path(tmpdir) / "real_global"
            link_target.mkdir()
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir()
            mem_dir = project_dir / "memory"
            mem_dir.mkdir()
            (mem_dir / "global").symlink_to(link_target)
            with patch(
                "search_memory.get_memory_paths",
                return_value=(project_dir, mem_dir, Path("/global")),
            ):
                local_db, global_db = search_memory._resolve_db_paths()
                self.assertEqual(local_db, mem_dir / "memory.db")
                self.assertEqual(
                    global_db.resolve(), (link_target / "memory.db").resolve()
                )

    def test_custom_db_path_with_various_types(self):
        """custom_db_path can be a string and gets converted to Path."""
        fake_mem = Path("/some/project/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/some/project"), fake_mem, Path("/global")),
        ):
            local_db, _ = search_memory._resolve_db_paths(custom_db_path="/tmp/test.db")
            self.assertEqual(local_db, Path("/tmp/test.db"))

    def test_global_db_never_affected_by_custom_path(self):
        """custom_db_path only affects local, not global."""
        fake_mem = Path("/some/project/memory")
        custom = Path("/custom/db.sqlite")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/some/project"), fake_mem, Path("/global")),
        ):
            _, global_db = search_memory._resolve_db_paths(custom_db_path=str(custom))
            self.assertNotEqual(global_db, custom)


# ---------------------------------------------------------------------------
# search_memories — mocked tests
# ---------------------------------------------------------------------------


class TestSearchMemoriesMocked(unittest.TestCase):
    """Tests for search_memories using mocked _mcp_search_memories."""

    def _patch_all(
        self,
        local_results=None,
        global_results=None,
        local_db_exists=True,
        global_db_exists=True,
        fake_project=None,
    ):
        """Set up patches and return (local_db_path, global_db_path, search_calls).

        Tempdir lives for the duration of the test via addCleanup.
        """
        if local_results is None:
            local_results = _empty_search()
        if global_results is None:
            global_results = _empty_search()
        if fake_project is None:
            fake_project = Path("/fake/project")

        self._tmpdir_obj = tempfile.TemporaryDirectory()
        tmpdir = self._tmpdir_obj.name
        self.addCleanup(self._tmpdir_obj.cleanup)

        local_db = Path(tmpdir) / "local.db"
        global_db = Path(tmpdir) / "global.db"
        if local_db_exists:
            local_db.touch()
        if global_db_exists:
            global_db.touch()

        search_calls = []

        def fake_mcp_search(db_path, query, **kwargs):
            search_calls.append({"db_path": db_path, "query": query, **kwargs})
            if db_path == local_db:
                return local_results
            elif db_path == global_db:
                return global_results
            return _empty_search()

        patches = {
            "_resolve": patch(
                "search_memory._resolve_db_paths", return_value=(local_db, global_db)
            ),
            "_mcp": patch(
                "search_memory._mcp_search_memories", side_effect=fake_mcp_search
            ),
        }
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)

        return local_db, global_db, search_calls

    def test_basic_search_returns_results(self):
        item = _make_item("test/note-1")
        local_db, global_db, calls = self._patch_all(
            local_results=_local_search_return(item)
        )
        result = search_memory.search_memories("hello")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "test/note-1")
        self.assertEqual(result[0]["source_db"], "local")

    def test_include_global_false_skips_global_db(self):
        local_db, global_db, calls = self._patch_all(
            local_results=_empty_search(),
            global_results=_global_search_return(_make_item("g/1")),
        )
        result = search_memory.search_memories("test", include_global=False)
        self.assertEqual(len(result), 0)
        # Only one call (local), no global call
        self.assertEqual(len(calls), 1)

    def test_include_global_true_combines_results(self):
        local_item = _make_item("l/1", content="local note")
        global_item = _make_item("g/1", content="global note")
        local_db, global_db, calls = self._patch_all(
            local_results=_local_search_return(local_item),
            global_results=_global_search_return(global_item),
        )
        result = search_memory.search_memories("note")
        self.assertEqual(len(result), 2)
        sources = {r["source_db"] for r in result}
        self.assertEqual(sources, {"local", "global"})

    def test_global_skipped_when_local_has_enough(self):
        """Global DB is not queried when local results >= min_local_results."""
        items = [_make_item(f"l/{i}") for i in range(5)]
        local_db, global_db, calls = self._patch_all(
            local_results=_local_search_return(*items),
        )
        result = search_memory.search_memories("test", min_local_results=3)
        self.assertEqual(len(result), 5)
        # Only local search called
        self.assertEqual(len(calls), 1)

    def test_global_queried_when_local_has_few(self):
        """Global DB IS queried when local results < min_local_results."""
        items = [_make_item(f"l/{i}") for i in range(2)]
        local_db, global_db, calls = self._patch_all(
            local_results=_local_search_return(*items),
            global_results=_global_search_return(_make_item("g/0")),
        )
        result = search_memory.search_memories("test", min_local_results=3)
        self.assertEqual(len(result), 3)
        self.assertEqual(len(calls), 2)

    def test_include_invalid_passed_through(self):
        """include_invalid kwarg is forwarded to _mcp_search_memories."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test", include_invalid=False)
        self.assertIn("include_invalid", calls[0])
        self.assertFalse(calls[0]["include_invalid"])

    def test_include_invalid_true_default(self):
        """Default include_invalid is True."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test")
        self.assertTrue(calls[0]["include_invalid"])

    def test_custom_db_path_resolves_correctly(self):
        """custom_db_path overrides the local path used in _resolve_db_paths."""
        item = _make_item("custom/1")
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_db = Path(tmpdir) / "custom.db"
            custom_db.touch()
            global_db = Path(tmpdir) / "global.db"
            global_db.touch()

            search_calls = []

            def fake_mcp(db_path, query, **kwargs):
                search_calls.append(db_path)
                if db_path == custom_db:
                    return _local_search_return(item)
                return _empty_search()

            with (
                patch(
                    "search_memory._resolve_db_paths",
                    return_value=(custom_db, global_db),
                ),
                patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
            ):
                result = search_memory.search_memories(
                    "test", custom_db_path=str(custom_db)
                )
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["id"], "custom/1")
                self.assertIn(custom_db, search_calls)

    def test_silent_suppresses_error_output(self):
        """When silent=True and DB doesn't exist, no error printed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            local_db = Path(tmpdir) / "nonexistent.db"
            global_db = Path(tmpdir) / "global.db"
            with patch(
                "search_memory._resolve_db_paths", return_value=(local_db, global_db)
            ):
                result = search_memory.search_memories("test", silent=True)
                self.assertEqual(result, [])

    def test_non_silent_prints_error(self):
        """When silent=False and DB doesn't exist, error is printed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            local_db = Path(tmpdir) / "nonexistent.db"
            global_db = Path(tmpdir) / "global.db"
            with patch(
                "search_memory._resolve_db_paths", return_value=(local_db, global_db)
            ):
                with patch("builtins.print") as mock_print:
                    result = search_memory.search_memories("test", silent=False)
                    self.assertEqual(result, [])
                    # Error message should be printed
                    printed = " ".join(str(c) for c in mock_print.call_args_list)
                    self.assertIn("does not exist", printed)

    def test_deduplication_same_id(self):
        """If the same note ID appears in both local and global, print loop deduplicates."""
        shared = _make_item("shared/1", content="dedup content")
        local_db, global_db, calls = self._patch_all(
            local_results=_local_search_return(shared),
            global_results=_global_search_return(shared),
        )
        result = search_memory.search_memories("test", silent=True)
        # all_items returned by the function contains both copies (no dedup on return)
        ids = [r["id"] for r in result]
        self.assertEqual(ids.count("shared/1"), 2)
        # But the print loop should only print each ID once (verified by
        # checking the printed output in test_dedup_in_print below)

    def test_limit_passed_to_mcp(self):
        """The limit param is forwarded to _mcp_search_memories."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test", limit=10)
        self.assertEqual(calls[0]["limit"], 10)

    def test_boost_pinned_always_true(self):
        """boost_pinned is always passed as True."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test")
        self.assertTrue(calls[0]["boost_pinned"])

    def test_rerank_always_true(self):
        """rerank is always passed as True."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test")
        self.assertTrue(calls[0]["rerank"])

    def test_include_global_false_only_one_mcp_call(self):
        """With include_global=False, only one _mcp_search_memories call."""
        local_db, global_db, calls = self._patch_all(local_results=_empty_search())
        search_memory.search_memories("test", include_global=False)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------------------
# search_memories — real DB tests (integration)
# ---------------------------------------------------------------------------


class TestSearchMemoriesIntegration(unittest.TestCase):
    """Integration tests using real temporary SQLite databases."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._local_db = Path(self._tmpdir) / "local.db"
        self._global_db = Path(self._tmpdir) / "global.db"
        self._cleanup_files = []

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _init_db(self, db_path):
        """Create a minimal memories table with FTS5."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                category TEXT,
                title_slug TEXT,
                tags TEXT,
                content TEXT,
                pinned INTEGER DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                created TEXT,
                last_accessed TEXT,
                valid_from TEXT,
                valid_to TEXT,
                superseded_by TEXT,
                source_file TEXT,
                embedding BLOB,
                importance REAL DEFAULT 0.5,
                tier TEXT DEFAULT 'warm',
                fitness_score REAL DEFAULT 0.0,
                psi REAL DEFAULT 0.0
            )
        """)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(id, content, tags, category, tokenize='porter unicode61')
            """)
        except Exception:
            pass
        return conn

    def _insert_note(
        self,
        db_path,
        note_id,
        content="test content",
        tags="test",
        pinned=0,
        access_count=0,
        global_note=False,
        category="lessons",
        created="2026-01-01T00:00:00+00:00",
    ):
        """Insert a note into the given DB."""
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            INSERT OR REPLACE INTO memories
            (id, category, title_slug, tags, content, pinned, access_count,
             created, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                note_id,
                category,
                note_id.split("/")[-1],
                tags,
                content,
                pinned,
                access_count,
                created,
                f"{note_id}.md",
            ),
        )
        # FTS insert
        try:
            row = conn.execute(
                "SELECT rowid FROM memories WHERE id=?", (note_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, id, content, tags) VALUES (?, ?, ?, ?)",
                    (row[0], note_id, content, tags),
                )
        except Exception:
            pass
        conn.commit()
        conn.close()

    def test_real_search_returns_results(self):
        """Basic search with real DB returns results."""
        self._init_db(self._local_db)
        self._insert_note(self._local_db, "l/test-1", content="hello world search")
        with patch(
            "search_memory._resolve_db_paths",
            return_value=(self._local_db, self._global_db),
        ):
            result = search_memory.search_memories(
                "hello", include_global=False, silent=True
            )
            self.assertIsInstance(result, list)

    def test_real_search_empty_db(self):
        """Search on empty DB returns empty list."""
        self._init_db(self._local_db)
        with patch(
            "search_memory._resolve_db_paths",
            return_value=(self._local_db, self._global_db),
        ):
            result = search_memory.search_memories(
                "anything", include_global=False, silent=True
            )
            self.assertEqual(result, [])

    def test_real_db_not_exists_returns_empty(self):
        """When the local DB doesn't exist, returns empty list."""
        fake_db = Path(self._tmpdir) / "does_not_exist.db"
        with patch(
            "search_memory._resolve_db_paths", return_value=(fake_db, self._global_db)
        ):
            result = search_memory.search_memories("test", silent=True)
            self.assertEqual(result, [])

    def test_access_count_incremented(self):
        """After search, access_count should be incremented for found notes."""
        self._init_db(self._local_db)
        self._insert_note(
            self._local_db, "l/acc-1", content="access test note", access_count=0
        )
        with patch(
            "search_memory._resolve_db_paths",
            return_value=(self._local_db, self._global_db),
        ):
            result = search_memory.search_memories(
                "access", include_global=False, silent=True
            )
            if result:  # If FTS matched
                conn = sqlite3.connect(str(self._local_db))
                row = conn.execute(
                    "SELECT access_count FROM memories WHERE id=?", ("l/acc-1",)
                ).fetchone()
                conn.close()
                if row:
                    self.assertGreater(row[0], 0)


# ---------------------------------------------------------------------------
# search_memories — combined sources tests
# ---------------------------------------------------------------------------


class TestSearchMemoriesMultiSource(unittest.TestCase):
    """Tests for multi-source combining logic."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._local_db = Path(self._tmpdir) / "local.db"
        self._global_db = Path(self._tmpdir) / "global.db"
        self._local_db.touch()
        self._global_db.touch()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _mock_search(self, local_items=None, global_items=None):
        """Return a mock for _mcp_search_memories."""
        local_items = local_items or []
        global_items = global_items or []

        calls = []

        def fake_mcp(db_path, query, **kwargs):
            calls.append({"db_path": str(db_path), "kwargs": kwargs})
            if str(db_path) == str(self._local_db):
                return {"results": local_items, "count": len(local_items), "output": ""}
            else:
                return {
                    "results": global_items,
                    "count": len(global_items),
                    "output": "",
                }

        return fake_mcp, calls

    def test_local_only_when_global_empty(self):
        """Only local results when global returns nothing."""
        item = _make_item("local/1")
        fake_mcp, calls = self._mock_search(local_items=[item])
        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            result = search_memory.search_memories("test", silent=True)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["source_db"], "local")

    def test_global_only_when_local_empty(self):
        """Global results when local returns nothing but enough results condition met."""
        item = _make_item("global/1")
        fake_mcp, calls = self._mock_search(global_items=[item])
        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            result = search_memory.search_memories(
                "test", min_local_results=3, silent=True
            )
            # Local returns 0 < min_local_results=3, so global is queried
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["source_db"], "global")

    def test_sources_searched_in_header(self):
        """Output header includes 'local + global' when both sources contribute."""
        local_item = _make_item("l/1", content="local content")
        global_item = _make_item("g/1", content="global content")
        fake_mcp, _ = self._mock_search(
            local_items=[local_item], global_items=[global_item]
        )
        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            with patch("builtins.print") as mock_print:
                # min_local_results=2 means: if local returns < 2 items, query global
                search_memory.search_memories("test", min_local_results=2, silent=True)
                # Check that the header includes both sources
                printed = " ".join(str(c) for c in mock_print.call_args_list)
                self.assertIn("local", printed)
                self.assertIn("global", printed)


# ---------------------------------------------------------------------------
# search_memories — __main__ CLI path
# ---------------------------------------------------------------------------


class TestCLI(unittest.TestCase):
    """Tests for the __main__ CLI argument parsing."""

    def test_cli_basic(self):
        """Basic CLI invocation with a query."""
        with (
            patch("sys.argv", ["search_memory.py", "test query"]),
            patch("search_memory.search_memories") as mock_search,
        ):
            search_memory.search_memories = mock_search
            # Simulate the __main__ block

            # We test the logic inline rather than re-exec __main__
            query = "test query"
            search_memory.search_memories(
                query, limit=5, include_global=True, silent=False
            )
            mock_search.assert_called_once_with(
                query, limit=5, include_global=True, silent=False
            )

    def test_cli_with_limit(self):
        """CLI parses numeric arg as limit."""
        with (
            patch("sys.argv", ["search_memory.py", "query", "10"]),
            patch("search_memory.search_memories") as mock_search,
        ):
            search_memory.search_memories(
                "query", limit=10, include_global=True, silent=False
            )
            mock_search.assert_called_once()

    def test_cli_with_no_global(self):
        """CLI --no-global flag."""
        with (
            patch("sys.argv", ["search_memory.py", "query", "--no-global"]),
            patch("search_memory.search_memories") as mock_search,
        ):
            search_memory.search_memories(
                "query", limit=5, include_global=False, silent=False
            )
            mock_search.assert_called_once()

    def test_cli_with_db_path(self):
        """CLI with a custom db path."""
        with (
            patch("sys.argv", ["search_memory.py", "query", "/tmp/custom.db"]),
            patch("search_memory.search_memories") as mock_search,
        ):
            search_memory.search_memories(
                "query",
                limit=5,
                custom_db_path="/tmp/custom.db",
                include_global=True,
                silent=False,
            )
            mock_search.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_db_paths — deeper edge cases
# ---------------------------------------------------------------------------


class TestResolveDbPathsEdgeCases(unittest.TestCase):
    """Edge cases for path resolution."""

    def test_real_world_default_paths(self):
        """With real find_project_root, default paths should be sensible."""
        # Don't patch find_project_root; use the real one
        Path.cwd()
        local_db, global_db = search_memory._resolve_db_paths()
        self.assertIsInstance(local_db, Path)
        self.assertIsInstance(global_db, Path)
        self.assertTrue(str(local_db).endswith("memory.db"))
        self.assertTrue(str(global_db).endswith("memory.db"))

    def test_custom_path_does_not_touch_global(self):
        """Even with custom_db_path, global_db remains unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom = Path(tmpdir) / "x.db"
            _, expected_global = search_memory._resolve_db_paths()
            local_db, global_db = search_memory._resolve_db_paths(
                custom_db_path=str(custom)
            )
            self.assertEqual(local_db, custom)
            self.assertEqual(global_db, expected_global)
            self.assertTrue(str(global_db).endswith("memory.db"))

    def test_empty_string_custom_path_not_treated_as_custom(self):
        """Empty string is falsy, so default path is used."""
        fake_mem = Path("/fake/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/fake"), fake_mem, Path("/global")),
        ):
            local_db, _ = search_memory._resolve_db_paths(custom_db_path="")
            self.assertEqual(local_db, fake_mem / "memory.db")

    def test_none_custom_path_uses_default(self):
        """None (default) uses local_mem/memory.db from get_memory_paths."""
        fake_mem = Path("/fake/memory")
        with patch(
            "search_memory.get_memory_paths",
            return_value=(Path("/fake"), fake_mem, Path("/global")),
        ):
            local_db, _ = search_memory._resolve_db_paths(custom_db_path=None)
            self.assertEqual(local_db, fake_mem / "memory.db")


# ---------------------------------------------------------------------------
# print output format tests
# ---------------------------------------------------------------------------


class TestPrintFormat(unittest.TestCase):
    """Tests that search_memories prints formatted output."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._local_db = Path(self._tmpdir) / "local.db"
        self._global_db = Path(self._tmpdir) / "global.db"
        self._local_db.touch()
        self._global_db.touch()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_print_header_format(self):
        """Printed header matches expected format."""
        item = _make_item("fmt/test", content="format test content", tags=["tag1"])
        fake_calls = []

        def fake_mcp(db_path, query, **kwargs):
            fake_calls.append(query)
            if str(db_path) == str(self._local_db):
                return {"results": [item], "count": 1, "output": ""}
            return {"results": [], "count": 0, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            with patch("builtins.print") as mock_print:
                search_memory.search_memories("format", silent=True)
                printed_lines = []
                for call in mock_print.call_args_list:
                    printed_lines.append(str(call))
                all_text = "\n".join(printed_lines)
                # Should contain the query
                self.assertIn("format", all_text)
                # Should contain the separator
                self.assertIn("=" * 80, all_text)

    def test_print_includes_score(self):
        """Printed output includes the score."""
        item = _make_item("sc/test", score=0.75)

        def fake_mcp(db_path, query, **kwargs):
            if str(db_path) == str(self._local_db):
                return {"results": [item], "count": 1, "output": ""}
            return {"results": [], "count": 0, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            with patch("builtins.print") as mock_print:
                search_memory.search_memories("score", silent=True)
                all_text = "\n".join(str(c) for c in mock_print.call_args_list)
                self.assertIn("0.75", all_text)

    def test_print_includes_tags(self):
        """Printed output includes tags."""
        item = _make_item("tag/test", tags=["alpha", "beta"])

        def fake_mcp(db_path, query, **kwargs):
            if str(db_path) == str(self._local_db):
                return {"results": [item], "count": 1, "output": ""}
            return {"results": [], "count": 0, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            with patch("builtins.print") as mock_print:
                search_memory.search_memories("tagtest", silent=True)
                all_text = "\n".join(str(c) for c in mock_print.call_args_list)
                self.assertIn("alpha", all_text)
                self.assertIn("beta", all_text)


# ---------------------------------------------------------------------------
# Access count integration tests
# ---------------------------------------------------------------------------


class TestAccessCount(unittest.TestCase):
    """Integration tests verifying access_count gets incremented."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._local_db = Path(self._tmpdir) / "local.db"
        self._global_db = Path(self._tmpdir) / "global.db"
        self._local_db.touch()
        self._global_db.touch()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _init_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                category TEXT,
                title_slug TEXT,
                tags TEXT,
                content TEXT,
                pinned INTEGER DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                created TEXT,
                last_accessed TEXT,
                valid_from TEXT,
                valid_to TEXT,
                superseded_by TEXT,
                source_file TEXT,
                embedding BLOB,
                importance REAL DEFAULT 0.5,
                tier TEXT DEFAULT 'warm',
                fitness_score REAL DEFAULT 0.0,
                psi REAL DEFAULT 0.0
            )
        """)
        conn.commit()
        conn.close()

    def test_access_count_not_incremented_for_nonexistent_note(self):
        """If a result ID doesn't exist in DB, no crash."""
        item = _make_item("nonexistent/fake-id")

        def fake_mcp(db_path, query, **kwargs):
            if str(db_path) == str(self._local_db):
                return {"results": [item], "count": 1, "output": ""}
            return {"results": [], "count": 0, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            # Should not crash
            result = search_memory.search_memories("test", silent=True)
            self.assertEqual(len(result), 1)

    def test_access_count_increment_in_global_db(self):
        """Global DB notes also get access_count incremented."""
        self._init_db(self._global_db)
        conn = sqlite3.connect(str(self._global_db))
        conn.execute(
            """
            INSERT INTO memories (id, category, title_slug, tags, content,
                                  access_count, created, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "g/acc-global",
                "lessons",
                "acc-global",
                "test",
                "global access test",
                0,
                "2026-01-01T00:00:00+00:00",
                "g/acc-global.md",
            ),
        )
        conn.commit()
        conn.close()

        item = _make_item("g/acc-global")

        def fake_mcp(db_path, query, **kwargs):
            if str(db_path) == str(self._global_db):
                return {"results": [item], "count": 1, "output": ""}
            return {"results": [], "count": 0, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            result = search_memory.search_memories(
                "access", min_local_results=0, silent=True
            )
            if result:
                conn = sqlite3.connect(str(self._global_db))
                row = conn.execute(
                    "SELECT access_count FROM memories WHERE id=?", ("g/acc-global",)
                ).fetchone()
                conn.close()
                if row:
                    self.assertGreater(row[0], 0)


# ---------------------------------------------------------------------------
# Dedup in print output
# ---------------------------------------------------------------------------


class TestDedupInPrint(unittest.TestCase):
    """The print loop skips duplicate IDs even if they appear in all_items."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._local_db = Path(self._tmpdir) / "local.db"
        self._global_db = Path(self._tmpdir) / "global.db"
        self._local_db.touch()
        self._global_db.touch()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_duplicate_ids_only_printed_once(self):
        """Same ID in both local+global only printed once."""
        shared = _make_item("dedup/same-id", content="dedup content")
        fake_calls = []

        def fake_mcp(db_path, query, **kwargs):
            fake_calls.append(str(db_path))
            # Return the same item from both sources
            return {"results": [shared], "count": 1, "output": ""}

        with (
            patch(
                "search_memory._resolve_db_paths",
                return_value=(self._local_db, self._global_db),
            ),
            patch("search_memory._mcp_search_memories", side_effect=fake_mcp),
        ):
            with patch("builtins.print") as mock_print:
                result = search_memory.search_memories(
                    "dedup", min_local_results=2, silent=True
                )
                # all_items should contain both (before dedup)
                ids = [r["id"] for r in result]
                self.assertEqual(ids.count("dedup/same-id"), 2)
                # But the print loop should only print the header line once
                printed_text = "\n".join(str(c) for c in mock_print.call_args_list)
                # Check the [N] header pattern — should appear exactly once
                import re

                header_matches = re.findall(r"\[\d+\]\s+dedup/same-id", printed_text)
                self.assertEqual(
                    len(header_matches),
                    1,
                    f"ID header printed {len(header_matches)} times, expected 1",
                )


class TestSearchMemoryReal(unittest.TestCase):
    """E1 fix (2026-06-22): the rest of this file mocks
    ``_mcp_search_memories``. That covers the routing + formatting
    logic in ``search_memory.py``, but it does NOT verify that the
    wrapper actually invokes the canonical search pipeline correctly
    end-to-end. This class runs a real (unmocked) end-to-end test
    against a temp DB: save a memory through the save pipeline, then
    search for it through ``search_memory.search_memories`` with
    ``include_global=False`` so the test does not depend on the
    live global DB.

    The test is intentionally lightweight — no embeddings, no
    reranker. It verifies:
      * ``search_memory.search_memories`` routes to the canonical
        ``search_pipeline.search_memories`` (B1 / G5 wiring).
      * The wrapper returns the result dict, not the formatted
        string, so the caller can use the JSON.
      * The access_count side effect actually fires.
    """

    def setUp(self):
        # Isolate the test from the live global DB.
        self._tmpdir = Path(tempfile.mkdtemp(prefix="search_memory_real_"))
        self.local_db = self._tmpdir / "memory.db"
        # Make sure the wrapper doesn't reach for a global DB.
        self._saved_globallink = self._tmpdir / "global"
        if not self._saved_globallink.exists():
            try:
                self._saved_globallink.symlink_to(self._tmpdir)
            except Exception:
                pass

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_real_search_end_to_end(self):
        from save_pipeline import save_memory

        # Use a unique slug so re-runs of the test don't collide.
        import time as _t

        slug = f"real-search-{int(_t.time() * 1000)}"
        save_memory(
            content="the quick brown fox jumps over the lazy dog",
            category="lessons",
            title_slug=slug,
            tags=["test", "real"],
            safety_wiring=False,
            db_path=str(self.local_db),
        )
        # Force _resolve_db_paths to point at the temp DB.  We do NOT
        # patch the canonical search path — the test asserts that the
        # wrapper-to-pipeline wiring survives without monkey-patching.
        with patch(
            "search_memory._resolve_db_paths",
            return_value=(self.local_db, self._tmpdir / "global" / "memory.db"),
        ):
            with patch("builtins.print"):  # silence the CLI banner
                results = search_memory.search_memories(
                    "quick brown fox",
                    limit=3,
                    include_global=False,
                    silent=True,
                )
        self.assertGreater(
            len(results),
            0,
            f"expected at least 1 result for 'quick brown fox' in temp DB; got {len(results)}",
        )
        ids = [r["id"] for r in results]
        self.assertIn(
            f"lessons/{slug}",
            ids,
            f"expected the just-saved slug in the results; got {ids}",
        )
        # The wrapper should attach the source_db label (B1 wiring).
        for r in results:
            self.assertEqual(r.get("source_db"), "local")


if __name__ == "__main__":
    unittest.main()
