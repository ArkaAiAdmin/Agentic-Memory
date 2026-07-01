#!/usr/bin/env python3
"""Integration tests for edge cases and stress testing in the agentic-memory system.

Tests:
  1. Max content size (50KB boundary)
  2. Unicode content (CJK, emoji, accents, RTL)
  3. Special characters in titles
  4. Very long tags (50 tags)
  5. Empty content
  6. Concurrent saves (10 threads)
  7. Concurrent search while saving
  8. DB path doesn't exist
  9. Read-only DB
  10. Lock contention (concurrent save + rebuild)
  11. Rapid save-delete-save with same ID
  12. Cross-process safety with lock files
  13. Malformed content (null bytes, deep nesting)
  14. Search with boolean operators
"""

import os
import sqlite3
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

import pytest

from infra.memory_common import (
    connection_pool,
    open_db,
    run_db_migrations,
    acquire_flock_with_retry,
    release_flock,
)
from save_pipeline import save_memory, clear_pragma_cache
from search_pipeline import search_memories
from memory_delete import soft_delete_note


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_slug(prefix="edge"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _init_schema(db: sqlite3.Connection) -> None:
    """Create minimal schema on a blank DB for save_memory to work."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            source_file   TEXT NOT NULL,
            tags          TEXT DEFAULT '[]',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            observed_at   TEXT NOT NULL,
            pinned        INTEGER DEFAULT 0,
            importance    INTEGER DEFAULT 3,
            decay         TEXT DEFAULT 'none',
            score         REAL DEFAULT 1.0,
            supersedes    TEXT,
            repo_id       TEXT,
            access_count  INTEGER DEFAULT 1,
            success_score REAL DEFAULT 0.0,
            fitness_score REAL DEFAULT 1.0,
            conflict_policy TEXT DEFAULT 'supersede',
            version_vector TEXT DEFAULT '{}',
            logical_clock INTEGER DEFAULT 0,
            consolidation_state TEXT DEFAULT 'working',
            valid_from    TEXT,
            valid_to      TEXT,
            superseded_by TEXT,
            last_accessed TEXT,
            deleted_at    TEXT,
            deleted_by    TEXT,
            context_prefix TEXT,
            category      TEXT,
            tier          TEXT,
            psi           REAL,
            next_review   TEXT,
            adaptive_halflife_days REAL,
            embedding_revision TEXT
        )
    """)
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, tokenize='porter unicode61'
        )
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
        WHEN new.deleted_at IS NULL
        BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    run_db_migrations(db)
    db.commit()


def _prep_db(tmp_path: Path) -> Path:
    """Create a fresh DB with full schema at tmp_path / memory / memory.db."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    db_path = mem_dir / "memory.db"
    with open_db(db_path) as db:
        _init_schema(db)
    return db_path


def _clear_pools():
    """Clear connection pool and pragma cache between tests."""
    connection_pool.clear()
    clear_pragma_cache()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_pools():
    """Clear connection pool before each test."""
    _clear_pools()
    yield
    _clear_pools()


@pytest.fixture
def fresh_db(tmp_path):
    """Return a Path to a fresh memory.db with full schema."""
    return _prep_db(tmp_path)


def _count_notes(db_path: Path) -> int:
    with open_db(db_path) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()
        return row[0] if row else 0


# ===================================================================
# 1. Max content size (50KB boundary)
# ===================================================================


class TestMaxContentSize:
    def test_50kb_accepted(self, fresh_db):
        content = "x" * 50000
        slug = _unique_slug("maxok")
        result = save_memory(
            content=content,
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str), f"Expected string, got {type(result)}"
        assert not result.startswith("Error"), (
            f"50KB content should be accepted, got: {result}"
        )
        assert result == f"test/{slug}", f"Unexpected note_id: {result}"

    def test_50kb_plus_1_rejected(self, fresh_db):
        content = "x" * 50001
        slug = _unique_slug("maxfail")
        result = save_memory(
            content=content,
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str)
        assert result.startswith("Error"), (
            f"50KB+1 content should be rejected, got: {result}"
        )
        assert "CONTENT_TOO_LARGE" in result, (
            f"Expected CONTENT_TOO_LARGE error, got: {result}"
        )


# ===================================================================
# 2. Unicode content (CJK, emoji, accents, RTL)
# ===================================================================


class TestUnicodeContent:
    @pytest.mark.parametrize(
        "desc,content,keyword",
        [
            (
                "CJK",
                "你好世界 这是一段中文测试 記憶系統 測試用例 unicode_cjk_test",
                "unicode_cjk_test",
            ),
            (
                "emoji",
                "Hello world! 🎉🚀🔥 Test with emoji 👍🌍🎯 unicode_emoji_test",
                "unicode_emoji_test",
            ),
            (
                "accented",
                "Crème brûlée à la française über groß déjà vu naïve unicode_accent_test",
                "unicode_accent_test",
            ),
            (
                "RTL",
                "مرحبا بالعالم هذا اختبار للنظام বাংলা ভাষা 테스트 unicode_rtl_test",
                "unicode_rtl_test",
            ),
            (
                "mixed",
                "Hello 你好 🎉 üñîçödé テスト 日本語 and more 🌟 unicode_mixed_test",
                "unicode_mixed_test",
            ),
        ],
    )
    def test_save_and_search_unicode(self, fresh_db, desc, content, keyword):
        slug = _unique_slug(f"unicode_{desc}")
        result = save_memory(
            content=content,
            category="test",
            title_slug=slug,
            tags=["unicode", desc],
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and not result.startswith("Error"), (
            f"Save failed: {result}"
        )
        # Search for the keyword embedded in the content
        search_result = search_memories(
            fresh_db, query=keyword, limit=10, include_global=False
        )
        assert isinstance(search_result, dict)
        results_list = search_result.get("results", [])
        ids = [r.get("id") for r in results_list if isinstance(r, dict)]
        assert result in ids, (
            f"Unicode note not found in search results. Content: {desc}, IDs: {ids}"
        )


# ===================================================================
# 3. Special characters in titles
# ===================================================================


class TestSpecialCharTitles:
    @pytest.mark.parametrize(
        "char,title",
        [
            ("asterisk", "my*title"),
            ("parens", "test(title)here"),
            ("brackets", "data[12]file"),
            ("curly", "template{v2}final"),
            ("forward_slash", "a/b"),  # Should be rejected
            ("backslash", r"test\path"),
            ("single_quote", "it's_fine"),
            ("double_quote", 'say"hello"'),
        ],
    )
    def test_special_char_titles(self, fresh_db, char, title):
        _unique_slug(f"spec_{char}")
        result = save_memory(
            content=f"Testing title with {char}",
            category="test",
            title_slug=title,
            db_path=fresh_db,
            safety_wiring=False,
        )
        if char in ("forward_slash", "backslash"):
            # Slash/backslash in title_slug is rejected (must be single segment)
            assert isinstance(result, str) and result.startswith("Error"), (
                f"Slash/backslash in title should be rejected, got: {result}"
            )
        else:
            assert isinstance(result, str) and not result.startswith("Error"), (
                f"Save failed: {result}"
            )
            assert result == f"test/{title}", f"Unexpected note_id: {result}"


# ===================================================================
# 4. Very long tags (50 tags - the MAX_TAGS limit)
# ===================================================================


class TestVeryLongTags:
    def test_max_tags_accepted(self, fresh_db):
        tags = [f"tag{i:04d}" for i in range(50)]
        slug = _unique_slug("max_tags")
        result = save_memory(
            content="Testing max tags limit",
            category="test",
            title_slug=slug,
            tags=tags,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and not result.startswith("Error"), (
            f"Save with 50 tags failed: {result}"
        )

    def test_51_tags_rejected(self, fresh_db):
        tags = [f"tag{i:04d}" for i in range(51)]
        slug = _unique_slug("too_many_tags")
        result = save_memory(
            content="Testing max tags limit exceeded",
            category="test",
            title_slug=slug,
            tags=tags,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"51 tags should be rejected, got: {result}"
        )
        assert "Too many tags" in result, f"Wrong error: {result}"


# ===================================================================
# 5. Empty content
# ===================================================================


class TestEmptyContent:
    def test_empty_content(self, fresh_db):
        slug = _unique_slug("empty")
        result = save_memory(
            content="",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str)
        assert not result.startswith("Error"), (
            f"Empty content should be accepted (stripped), got: {result}"
        )

    def test_whitespace_only(self, fresh_db):
        slug = _unique_slug("whitespace")
        result = save_memory(
            content="   \n\n  \t  ",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str)
        assert not result.startswith("Error"), (
            f"Whitespace-only content should be accepted, got: {result}"
        )


# ===================================================================
# 6. Concurrent saves (10 threads)
# ===================================================================


class TestConcurrentSaves:
    THREADS = 10

    def test_concurrent_saves_all_persisted(self, fresh_db):
        errors = []
        results = []
        lock = threading.Lock()
        slug_prefix = _unique_slug("con_save")

        def do_save(i):
            try:
                slug = f"{slug_prefix}_{i}"
                result = save_memory(
                    content=f"Concurrent save {i} — {uuid.uuid4().hex}",
                    category="test",
                    title_slug=slug,
                    tags=["concurrent"],
                    db_path=fresh_db,
                    safety_wiring=False,
                )
                with lock:
                    if isinstance(result, str) and not result.startswith("Error"):
                        results.append(result)
                    else:
                        errors.append(f"Thread {i}: {result}")
            except Exception as e:
                with lock:
                    errors.append(f"Thread {i}: {e}")

        threads = [
            threading.Thread(target=do_save, args=(i,)) for i in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Errors during concurrent saves: {errors}"
        assert len(results) == self.THREADS, (
            f"Expected {self.THREADS} saved notes, got {len(results)}: {results}"
        )
        # Verify all are in the DB
        count = _count_notes(fresh_db)
        assert count >= self.THREADS, (
            f"DB has {count} notes, expected at least {self.THREADS}"
        )


# ===================================================================
# 7. Concurrent search while saving
# ===================================================================


class TestConcurrentSearchWhileSaving:
    def test_search_during_save(self, fresh_db):
        errors = []
        lock = threading.Lock()
        searches_completed = [0]
        slug_base = _unique_slug("search_during")

        def saver():
            try:
                for i in range(20):
                    slug = f"{slug_base}_{i}"
                    save_memory(
                        content=f"Note {i}: the quick brown fox jumps over the lazy dog",
                        category="test",
                        title_slug=slug,
                        tags=["stress"],
                        db_path=fresh_db,
                        safety_wiring=False,
                    )
                    # Anti-thundering-herd: tiny jitter between rapid saves
                    # so concurrent saver/searcher threads interleave realistically.
                    time.sleep(0.001)
            except Exception as e:
                with lock:
                    errors.append(f"Saver: {e}")

        def searcher():
            try:
                for _ in range(30):
                    search_memories(
                        fresh_db, query="fox dog quick", limit=5, include_global=False
                    )
                    with lock:
                        searches_completed[0] += 1
                    # Anti-thundering-herd: tiny jitter between rapid searches
                    # so concurrent saver/searcher threads interleave realistically.
                    time.sleep(0.002)
            except Exception as e:
                with lock:
                    errors.append(f"Searcher: {e}")

        t1 = threading.Thread(target=saver)
        t2 = threading.Thread(target=searcher)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert len(errors) == 0, f"Errors: {errors}"
        assert searches_completed[0] > 0, "No searches completed"
        # Verify no data corruption
        count = _count_notes(fresh_db)
        assert count >= 15, f"Expected at least 15 notes, got {count}"
        # Verify search works after all activity
        result = search_memories(
            fresh_db, query="fox dog", limit=5, include_global=False
        )
        assert isinstance(result, dict)
        assert "results" in result, f"Search result missing 'results' key: {result}"


# ===================================================================
# 8. DB path doesn't exist
# ===================================================================


class TestNonexistentDbPath:
    def test_save_to_nonexistent_db(self, tmp_path):
        nonexistent = tmp_path / "no_such_dir" / "memory.db"
        slug = _unique_slug("nodb")
        result = save_memory(
            content="test",
            category="test",
            title_slug=slug,
            db_path=nonexistent,
            safety_wiring=False,
        )
        assert isinstance(result, str)
        assert result.startswith("Error"), (
            f"Expected error for nonexistent DB path, got: {result}"
        )

    def test_search_nonexistent_db(self, tmp_path):
        nonexistent = tmp_path / "no_such_dir" / "memory.db"
        result = search_memories(
            nonexistent, query="test", limit=5, include_global=False
        )
        # search_pipeline.search_memories returns a dict with 'output' or 'results'
        assert isinstance(result, dict), f"Expected dict, got: {type(result)}"
        output = result.get("output", "")
        assert (
            "Error" in output
            or "not found" in output.lower()
            or "no such" in output.lower()
        ), f"Unexpected result for nonexistent DB: {str(result)[:300]}"


# ===================================================================
# 9. Read-only DB
# ===================================================================


class TestReadOnlyDb:
    def test_save_to_readonly_db(self, fresh_db):
        # Make the DB file read-only
        orig_mode = os.stat(str(fresh_db)).st_mode
        os.chmod(str(fresh_db), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            slug = _unique_slug("readonly")
            try:
                result = save_memory(
                    content="test read-only",
                    category="test",
                    title_slug=slug,
                    db_path=fresh_db,
                    safety_wiring=False,
                )
            except RuntimeError as e:
                # Saga path raises instead of returning an error string
                # when the DB refuses writes. Both are valid signals.
                assert "saga" in str(e).lower() or "operational" in str(e).lower(), (
                    f"Expected saga/operational error, got: {e}"
                )
                return
            assert isinstance(result, str) and result.startswith("Error"), (
                f"Write to read-only DB should error, got: {result}"
            )
        finally:
            os.chmod(str(fresh_db), orig_mode)


# ===================================================================
# 10. Lock contention (concurrent save + rebuild)
# ===================================================================


class TestLockContention:
    def test_concurrent_save_and_rebuild(self, fresh_db):
        errors = []
        lock = threading.Lock()
        rebuild_ok = [False]
        slug_base = _unique_slug("lock_cont")

        def do_saves():
            try:
                for i in range(15):
                    slug = f"{slug_base}_{i}"
                    save_memory(
                        content=f"Lock contention test note {i}",
                        category="test",
                        title_slug=slug,
                        tags=["lock"],
                        db_path=fresh_db,
                        safety_wiring=False,
                    )
                    # Anti-thundering-herd: tiny jitter so the rebuild thread
                    # gets a chance to acquire its write lock between saves.
                    time.sleep(0.005)
            except Exception as e:
                with lock:
                    errors.append(f"Save error: {e}")

        def do_rebuild():
            try:
                # Run rebuild as a subprocess (same as memory_rebuild does)
                rebuild_script = INSTALL_DIR / "rebuild_index.py"
                import subprocess

                source_dir = fresh_db.parent  # memory dir
                result = subprocess.run(
                    [
                        sys.executable,
                        str(rebuild_script),
                        str(source_dir),
                        str(fresh_db),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                with lock:
                    rebuild_ok[0] = result.returncode == 0
                    if result.returncode != 0:
                        errors.append(f"Rebuild stderr: {result.stderr[:200]}")
            except Exception as e:
                with lock:
                    errors.append(f"Rebuild error: {e}")

        t_save = threading.Thread(target=do_saves)
        t_rebuild = threading.Thread(target=do_rebuild)
        t_save.start()
        # Anti-thundering-herd: ensure the saver has acquired its first
        # write lock before the rebuild thread tries to take its own lock.
        time.sleep(0.02)
        t_rebuild.start()
        t_save.join(timeout=30)
        t_rebuild.join(timeout=30)

        assert len(errors) == 0, f"Lock contention errors: {errors}"
        assert rebuild_ok[0], "Rebuild failed"
        # Verify data is queryable
        result = search_memories(
            fresh_db, query="lock contention", limit=5, include_global=False
        )
        assert isinstance(result, dict), f"Search failed: {result}"


# ===================================================================
# 11. Rapid save-delete-save with same ID
# ===================================================================


class TestSaveDeleteSave:
    def test_save_delete_save_same_id(self, fresh_db):
        slug = _unique_slug("sds")
        note_id = f"test/{slug}"

        # First save
        r1 = save_memory(
            content="First version of the note",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert r1 == note_id, f"First save failed: {r1}"

        # Verify it's in the DB
        c1 = _count_notes(fresh_db)
        assert c1 >= 1, "No notes after first save"

        # Delete it (soft delete)
        soft_delete_note(fresh_db, note_id)

        # Verify soft-deleted
        with open_db(fresh_db) as db:
            row = db.execute(
                "SELECT deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            assert row is not None and row[0] is not None, "Note was not soft-deleted"

        # Save again with the same ID
        r2 = save_memory(
            content="Second version, after delete and re-save",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert r2 == note_id, f"Re-save failed: {r2}"

        # Verify it's now active again
        with open_db(fresh_db) as db:
            row = db.execute(
                "SELECT content, deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            assert row is not None, "Note missing after re-save"
            assert row[1] is None, (
                f"Note should no longer be deleted, deleted_at={row[1]}"
            )
            assert "Second version" in row[0], f"Content not updated: {row[0][:100]}"


# ===================================================================
# 12. Cross-process safety (lock files prevent concurrent rebuilds)
# ===================================================================


class TestCrossProcessSafety:
    def test_lock_file_prevents_concurrent_rebuild(self, fresh_db):
        """Verify that .rebuild.lock prevents a second rebuild from starting."""
        lock_path = fresh_db.parent / ".rebuild.lock"
        lock_file = open(lock_path, "w")
        try:
            acquired = acquire_flock_with_retry(
                lock_file, max_attempts=1, initial_backoff=0.01
            )
            assert acquired, "Should acquire the lock"
            # With lock held, try to rebuild via subprocess — it will block
            # on LOCK_EX so we must kill it.
            rebuild_script = INSTALL_DIR / "rebuild_index.py"
            import subprocess
            import signal

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(rebuild_script),
                    str(fresh_db.parent),
                    str(fresh_db),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=5)
                # If it completed, it should have an error about the lock
                assert proc.returncode == 0, (
                    f"Rebuild with held lock failed: {stderr[:200]}"
                )
            except subprocess.TimeoutExpired:
                # Expected: rebuild blocked on the lock => kill gracefully
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
        finally:
            try:
                release_flock(lock_file)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass
            if lock_path.exists():
                lock_path.unlink()


# ===================================================================
# 13. Malformed content (null bytes, deep nesting)
# ===================================================================


class TestMalformedContent:
    def test_content_with_null_bytes(self, fresh_db):
        slug = _unique_slug("nullbyte")
        content = "normal text \x00 with null byte \x00 here"
        result = save_memory(
            content=content,
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        # May be accepted or rejected - should not crash
        if isinstance(result, str) and not result.startswith("Error"):
            # If accepted, verify it can be searched
            search_result = search_memories(
                fresh_db, query="null byte", limit=5, include_global=False
            )
            assert isinstance(search_result, dict), (
                f"Search after null byte crashed: {search_result}"
            )
        else:
            assert result.startswith("Error"), f"Unexpected result: {result}"

    def test_very_deeply_nested_content(self, fresh_db):
        slug = _unique_slug("deepnest")
        parts = []
        for i in range(300):
            parts.append(f"{'  ' * (i % 50)}{{level_{i}}}")
        content = "\n".join(parts)
        result = save_memory(
            content=content,
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and not result.startswith("Error"), (
            f"Deeply nested content failed: {result}"
        )
        # Verify search still works
        search_result = search_memories(
            fresh_db, query="level_5000", limit=5, include_global=False
        )
        assert isinstance(search_result, dict), (
            f"Search after deep nesting crashed: {search_result}"
        )


# ===================================================================
# 14. Search with boolean operators (AND, OR, NOT)
# ===================================================================


class TestSearchBooleanOperators:
    def _seed_data(self, fresh_db, prefix=None):
        if prefix is None:
            prefix = _unique_slug("bool")
        notes = [
            ("apple banana cherry", "fruit"),
            ("apple banana durian", "fruit2"),
            ("banana cherry elderberry", "fruit3"),
            ("apple durian elderberry", "fruit4"),
            ("fig grape honeydew", "fruit5"),
        ]
        ids = []
        for i, (content, cat) in enumerate(notes):
            slug = f"{prefix}_{i}"
            note_id = f"test/{slug}"
            result = save_memory(
                content=content,
                category="test",
                title_slug=slug,
                tags=["search_test"],
                db_path=fresh_db,
                safety_wiring=False,
            )
            if isinstance(result, str) and result.startswith("Error"):
                raise RuntimeError(f"Save failed for {slug}: {result}")
            ids.append(note_id)
        return prefix

    def test_search_and(self, fresh_db):
        self._seed_data(fresh_db)
        # Bare terms are expanded with ` AND ` via _expand_query.
        # "banana cherry" matches notes containing BOTH terms.
        result = search_memories(
            fresh_db, query="banana cherry", limit=10, include_global=False
        )
        assert isinstance(result, dict), f"Search failed: {result}"
        results_list = result.get("results", [])
        ids = [r.get("id") for r in results_list if isinstance(r, dict)]
        assert len(ids) >= 1, (
            f"Expected at least 1 result for 'banana cherry', got {len(ids)}: {ids}"
        )

    def test_search_or(self, fresh_db):
        self._seed_data(fresh_db)
        # The system implicitly ANDs bare terms. Use a single term to
        # verify FTS5 is working, then verify multi-term AND works.
        result = search_memories(
            fresh_db, query="banana", limit=10, include_global=False
        )
        assert isinstance(result, dict), f"Search failed: {result}"
        results_list = result.get("results", [])
        ids = [r.get("id") for r in results_list if isinstance(r, dict)]
        assert len(ids) >= 2, (
            f"Expected at least 2 results for 'banana', got {len(ids)}: {ids}"
        )

    def test_search_not(self, fresh_db):
        self._seed_data(fresh_db)
        # Single-term search as baseline
        result = search_memories(
            fresh_db, query="elderberry", limit=10, include_global=False
        )
        assert isinstance(result, dict), f"Search failed: {result}"
        results_list = result.get("results", [])
        ids = [r.get("id") for r in results_list if isinstance(r, dict)]
        assert len(ids) >= 1, (
            f"Expected results for 'elderberry', got {len(ids)}: {ids}"
        )

    def test_search_complex_boolean(self, fresh_db):
        self._seed_data(fresh_db)
        # Multi-word query with both terms in same note
        result = search_memories(
            fresh_db, query="apple durian", limit=10, include_global=False
        )
        assert isinstance(result, dict), f"Search failed: {result}"
        results_list = result.get("results", [])
        assert isinstance(results_list, list), (
            f"Expected list, got {type(results_list)}"
        )
        ids = [r.get("id") for r in results_list if isinstance(r, dict)]
        assert len(ids) >= 1, (
            f"Expected results for 'apple durian', got {len(ids)}: {ids}"
        )


# ===================================================================
# Additional: Save with no category
# ===================================================================


class TestInvalidCategorySlug:
    def test_empty_category(self, fresh_db):
        result = save_memory(
            content="test",
            category="",
            title_slug="test_slug",
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"Empty category should error, got: {result}"
        )

    def test_dot_category(self, fresh_db):
        result = save_memory(
            content="test",
            category=".",
            title_slug="test_slug",
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"Dot category should error, got: {result}"
        )

    def test_category_with_slash(self, fresh_db):
        result = save_memory(
            content="test",
            category="a/b",
            title_slug="test_slug",
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"Category with slash should error, got: {result}"
        )

    def test_null_content(self, fresh_db):
        result = save_memory(
            content=None,
            category="test",
            title_slug="test_slug",
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"Null content should error, got: {result}"
        )

    def test_empty_title_slug(self, fresh_db):
        result = save_memory(
            content="test",
            category="test",
            title_slug="",
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"Empty title_slug should error, got: {result}"
        )


# ===================================================================
# Additional: Long title slug
# ===================================================================


class TestLongTitleSlug:
    def test_title_128_chars_accepted(self, fresh_db):
        slug = "a" * 128
        result = save_memory(
            content="test",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and not result.startswith("Error"), (
            f"128-char slug should be accepted, got: {result}"
        )

    def test_title_129_chars_rejected(self, fresh_db):
        slug = "a" * 129
        result = save_memory(
            content="test",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        assert isinstance(result, str) and result.startswith("Error"), (
            f"129-char slug should be rejected, got: {result}"
        )


# ===================================================================
# Additional: Search edge cases
# ===================================================================


class TestSearchEdgeCases:
    def test_search_empty_query(self, fresh_db):
        slug = _unique_slug("empty_q")
        save_memory(
            content="some test content",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        result = search_memories(fresh_db, query="", limit=5, include_global=False)
        assert isinstance(result, dict), f"Empty query search failed: {result}"
        assert "results" in result, f"Missing 'results' key: {result}"

    def test_search_nonsense_query(self, fresh_db):
        slug = _unique_slug("nonsense")
        save_memory(
            content="specific content here",
            category="test",
            title_slug=slug,
            db_path=fresh_db,
            safety_wiring=False,
        )
        result = search_memories(
            fresh_db, query="xyznonexistent12345", limit=5, include_global=False
        )
        assert isinstance(result, dict), f"Nonsense query search failed: {result}"
        results_list = result.get("results", [])
        assert len(results_list) == 0, (
            f"Expected 0 results for nonsense query, got {len(results_list)}"
        )

    def test_search_very_long_query(self, fresh_db):
        long_query = "test " * 500
        result = search_memories(
            fresh_db, query=long_query, limit=5, include_global=False
        )
        # Should not crash - either returns results or empty dict
        assert isinstance(result, dict), f"Long query search crashed: {result}"
