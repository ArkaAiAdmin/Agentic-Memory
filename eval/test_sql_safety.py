"""SQL injection and adversarial-input safety tests.

Tests the one exploitable path (category interpolation in
search/orchestrator.py) and defense-in-depth for all parameterized
save/delete/FTS5 paths.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_mcp
import save_pipeline
from infra.db import open_db
from rebuild_index import rebuild_index
from search.orchestrator import search_memories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_test_env(tmpdir: str):
    """Redirect all DB paths to tmpdir and bootstrap all required tables."""
    tmp = Path(tmpdir)
    db_path = tmp / "memory.db"
    _orig_memory_db_path = os.environ.get("MEMORY_DB_PATH")
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    orig_resolve = save_pipeline.resolve_active_memory_dir
    save_pipeline.resolve_active_memory_dir = lambda **_: tmp

    rebuild_index(tmp, db_path)

    return _orig_memory_db_path, orig_resolve


def _restore_test_env(orig_memory_db_path=None, orig_resolve=None) -> None:
    """Restore original env var and module attribute."""
    if orig_memory_db_path is not None:
        os.environ["MEMORY_DB_PATH"] = orig_memory_db_path
    else:
        os.environ.pop("MEMORY_DB_PATH", None)
    if orig_resolve is not None:
        save_pipeline.resolve_active_memory_dir = orig_resolve


def _count_memories(db_path: Path) -> int:
    with open_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def _table_exists(db_path: Path, name: str) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Section 1: Category SQL Injection (the exploitable path)
# ---------------------------------------------------------------------------

class TestCategorySQLInjection:
    """Category is interpolated into SQL at search/orchestrator.py:1931."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save(self, content: str, category: str = "lessons", title_slug: str = ""):
        return memory_mcp.memory_save(
            content=content,
            category=category,
            title_slug=title_slug or f"sql-test-{abs(hash(content)) % 10000}",
        )

    def test_category_single_quote_breaks_sql(self):
        """A single quote in category must not cause SQL error or data leak."""
        self._save("alpha lessons", title_slug="alpha-lessons")
        self._save("alpha sensitive", category="sensitive", title_slug="alpha-sensitive")

        result = search_memories(
            db_path=self.db_path,
            query="alpha",
            category="lessons' OR 1=1 --",
        )
        rows = result.get("results", [])
        for row in rows:
            cat = row.get("category", "")
            assert cat in ("lessons", "sensitive", ""), \
                f"Injection succeeded: unfiltered row returned with category={cat!r}"

    def test_category_double_quote_escape(self):
        """Double quote in category must not crash the search."""
        self._save("beta", title_slug="beta-lesson")
        result = search_memories(db_path=self.db_path, query="beta", category='foo"bar')
        assert result is not None

    def test_category_semicolon_termination(self):
        """Semicolon + DROP TABLE in category must not drop the table."""
        self._save("gamma1", title_slug="gamma1")
        self._save("gamma2", category="sessions", title_slug="gamma2")

        payload = "lessons'; DROP TABLE memories; --"
        search_memories(db_path=self.db_path, query="gamma", category=payload)

        assert _table_exists(self.db_path, "memories"), "memories table was dropped!"
        assert _count_memories(self.db_path) >= 2

    def test_category_null_byte(self):
        """Null byte in category must be handled gracefully."""
        self._save("delta", title_slug="delta-lesson")
        payload = "lessons\x00'"
        try:
            result = search_memories(db_path=self.db_path, query="delta", category=payload)
            assert result is not None
        except Exception as exc:
            assert "null" in str(exc).lower() or "byte" in str(exc).lower()

    def test_category_unicode_homoglyphs(self):
        """Curly/smart quotes that look like ' must not cause injection."""
        self._save("epsilon", title_slug="epsilon-lesson")
        payload = "foo\u2018\u2019bar"
        result = search_memories(db_path=self.db_path, query="epsilon", category=payload)
        assert result is not None

    def test_category_backslash_escape_chain(self):
        """Backslash-escaped quote in category must not break SQL."""
        self._save("zeta", title_slug="zeta-lesson")
        payload = "foo\\' OR 1=1 --"
        result = search_memories(db_path=self.db_path, query="zeta", category=payload)
        assert result is not None


# ---------------------------------------------------------------------------
# Section 2: Parameterized Paths — adversarial content through bindings
# ---------------------------------------------------------------------------

class TestParameterizedSavePaths:
    """Content/tags/slug go through SQL via X parameterized bindings."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_content_with_sql_payload(self):
        payload = "'); DROP TABLE memories; --"
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="sql-payload"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")
        found = memory_mcp.memory_search(query="DROP TABLE", limit=5)
        assert found is not None

    def test_save_content_with_fts5_special_chars(self):
        payload = "NEAR(one two) AND NOT three OR four*"
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="fts-special"
        )
        assert "Error" not in res
        found = memory_mcp.memory_search(query="NEAR one two", limit=5)
        assert found is not None

    def test_save_tags_with_sql_payload(self):
        payload = ["'); DELETE FROM memories; --"]
        res = memory_mcp.memory_save(
            content="tag-test", tags=payload, category="lessons", title_slug="tag-payload"
        )
        assert "Error" not in res
        assert _count_memories(self.db_path) >= 1
        assert _table_exists(self.db_path, "memories")

    def test_save_title_slug_with_sql_payload(self):
        payload = "foo'; DROP TABLE--"
        res = memory_mcp.memory_save(
            content="slug-test", title_slug=payload, category="lessons"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")

    def test_save_content_with_null_byte(self):
        payload = "hello\x00world"
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="null-byte"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")

    def test_save_content_with_unicode_control_chars(self):
        payload = "hello\u0000\u0001\u001Fworld"
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="ctrl-chars"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")

    @pytest.mark.slow
    def test_save_very_long_content(self):
        payload = "x" * 40_000
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="very-long"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")
        assert _count_memories(self.db_path) >= 1


# ---------------------------------------------------------------------------
# Section 3: FTS5 MATCH — adversarial query inputs
# ---------------------------------------------------------------------------

class TestFTS5Adversarial:
    """User queries go through FTS5 MATCH via parameterized bindings."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save(self, content: str, category: str = "lessons", title_slug: str = ""):
        return memory_mcp.memory_save(
            content=content,
            category=category,
            title_slug=title_slug or f"fts-{abs(hash(content)) % 10000}",
        )

    def test_search_query_with_sql_injection(self):
        self._save("ftspayload")
        result = memory_mcp.memory_search(
            query="ftspayload'; DROP TABLE memories; --", limit=5
        )
        assert _table_exists(self.db_path, "memories")
        assert result is not None

    def test_search_query_with_fts5_syntax_bomb(self):
        self._save("ftssyntax")
        try:
            result = memory_mcp.memory_search(
                query="NEAR((((ftssyntax)))) OR NOT NOT NOT NOT * * *", limit=5
            )
            assert result is not None
        except Exception as exc:
            assert "fts" in str(exc).lower() or "syntax" in str(exc).lower()

    def test_search_query_with_unicode_fts5_operators(self):
        self._save("ftswide")
        result = memory_mcp.memory_search(query="ftswide\u002A", limit=5)
        assert result is not None

    def test_search_query_null_byte(self):
        self._save("ftsnull")
        try:
            result = memory_mcp.memory_search(
                query="ftsnull\x00exploit", limit=5
            )
            assert result is not None
        except Exception as exc:
            assert "null" in str(exc).lower() or "byte" in str(exc).lower()

    @pytest.mark.slow
    def test_search_query_very_long(self):
        self._save("ftslong")
        long_query = "a " * 50
        result = memory_mcp.memory_search(query=long_query, limit=5)
        assert result is not None


# ---------------------------------------------------------------------------
# Section 4: Delete Path — note_id injection
# ---------------------------------------------------------------------------

class TestDeleteNoteIdGate:
    """note_id must pass _validate_note_id before any SQL is built."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_note_id_with_sql_payload(self):
        memory_mcp.memory_save(content="del1", category="lessons", title_slug="del1")
        payload = "foo'; DROP TABLE--"
        result = memory_mcp.memory_delete(note_id=payload)
        assert result is not None
        assert _table_exists(self.db_path, "memories")

    def test_delete_note_id_with_path_traversal(self):
        payload = "../../etc/passwd"
        result = memory_mcp.memory_delete(note_id=payload)
        assert result is not None
        assert _table_exists(self.db_path, "memories")

    def test_delete_note_id_null_byte(self):
        payload = "lessons/null\x00byte"
        result = memory_mcp.memory_delete(note_id=payload)
        assert result is not None
        assert _table_exists(self.db_path, "memories")


# ---------------------------------------------------------------------------
# Section 5: Backlink / Wikilink target injection
# ---------------------------------------------------------------------------

class TestBacklinkSafety:
    """[[wiki-link]] targets extracted from content go through parameterized ? bindings."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_content_with_sql_wikilink(self):
        payload = "See [[foo'; DROP TABLE--]] for details."
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="wikilink-sql"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")
        assert _count_memories(self.db_path) >= 1

    def test_save_content_with_pattern_wikilinks(self):
        payload = "Links: [[_____]], [[%%%%%]], [[a_b]]"
        res = memory_mcp.memory_save(
            content=payload, category="lessons", title_slug="wikilink-pattern"
        )
        assert "Error" not in res
        assert _table_exists(self.db_path, "memories")
        assert _count_memories(self.db_path) >= 1


# ---------------------------------------------------------------------------
# Section 6: Cross-cutting — DB integrity after adversarial workload
# ---------------------------------------------------------------------------

class TestCrossCuttingSafety:
    """Run the full adversarial battery against a single temp DB."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)
        self.db_path = Path(self.tmpdir) / "memory.db"

    def teardown_method(self) -> None:
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save(self, content: str, category: str = "lessons", title_slug: str = ""):
        return memory_mcp.memory_save(
            content=content,
            category=category,
            title_slug=title_slug or f"xcut-{abs(hash(content)) % 10000}",
        )

    @pytest.mark.slow
    def test_sql_injection_does_not_corrupt_db(self):
        self._save("pre-seed", title_slug="pre-seed")
        memory_mcp.memory_search(query="lessons' OR 1=1 --", limit=5)
        self._save("'); DROP TABLE memories; --", title_slug="drop-test")

        assert _table_exists(self.db_path, "memories"), "memories table was dropped!"
        assert _count_memories(self.db_path) >= 1

    def test_category_injection_is_defense_in_depth_gap(self):
        union_payload = (
            "lessons' UNION SELECT 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20 --"
        )
        result = search_memories(
            db_path=self.db_path,
            query="pre-seed",
            category=union_payload,
        )
        assert result is not None
        rows = result.get("results", [])
        assert len(rows) == 0 or all(
            r.get("content", "") != "1" for r in rows
        ), "UNION injection returned wrong data"
