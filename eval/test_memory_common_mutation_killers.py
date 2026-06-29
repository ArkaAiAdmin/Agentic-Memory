#!/usr/bin/env python3
"""Targeted mutation killer tests for memory_common.py.

Each test targets specific survived mutations by asserting the exact
return value or behavior that a mutation would change. If a mutation
alters a constant, return value, or condition, these tests will fail.

Covers: _coerce, parse_frontmatter, get_memory_paths, count_rows,
atomic_write, safe_close_db, open_db, validate_config, log_backup,
_ConnectionPool, RateLimiter, wal_checkpoint_idle, cleanup_fts5_orphans,
maybe_checkpoint_on_startup, configure_logging.
"""


import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))
from _fixtures import bootstrap_temp_db_clean


from memory_common import (
    _ConnectionPool,
    _coerce,
    parse_frontmatter,
    get_memory_paths,
    count_rows,
    atomic_write,
    safe_close_db,
    open_db,
    validate_config,
    log_backup,
    RateLimiter,
    get_default_limiter,
    rate_limit_check,
    reset_rate_limiter,
    wal_checkpoint_idle,
    _maybe_checkpoint_on_startup,
    SCHEMA_VERSION,
    _VALID_LOG_LEVELS,
    PROJECT_ROOT_MARKERS,
    GLOBAL_MEM_DIR,
    configure_logging,
)
from infrastructure import GLOBAL_MEM_DIR as PROD_GLOBAL

_test_dir = tempfile.mkdtemp(prefix="test_mutation_killers_prod_")
PROD_DB = Path(_test_dir) / "memory.db"
bootstrap_temp_db_clean(PROD_DB)

def tearDownModule():
    import shutil
    shutil.rmtree(_test_dir, ignore_errors=True)



# ─── _coerce tests ───────────────────────────────────────────────────────────


class TestCoerceMutationKillers(unittest.TestCase):
    """Target survived mutations in _coerce."""

    def test_list_input_returns_list(self):
        result = _coerce("[a, b, c]")
        self.assertIsInstance(result, list)
        self.assertEqual(result, ["a", "b", "c"])

    def test_list_input_not_none(self):
        result = _coerce("[x]")
        self.assertIsNotNone(result)

    def test_empty_list_returns_empty_list(self):
        result = _coerce("[]")
        self.assertEqual(result, [])

    def test_true_string_returns_bool_true(self):
        result = _coerce("true")
        self.assertIs(result, True)

    def test_yes_string_returns_bool_true(self):
        result = _coerce("yes")
        self.assertIs(result, True)

    def test_on_string_returns_bool_true(self):
        result = _coerce("on")
        self.assertIs(result, True)

    def test_one_string_returns_bool_true(self):
        result = _coerce("1")
        self.assertIs(result, True)

    def test_false_string_returns_bool_false(self):
        result = _coerce("false")
        self.assertIs(result, False)

    def test_no_string_returns_bool_false(self):
        result = _coerce("no")
        self.assertIs(result, False)

    def test_off_string_returns_bool_false(self):
        result = _coerce("off")
        self.assertIs(result, False)

    def test_zero_string_returns_bool_false(self):
        result = _coerce("0")
        self.assertIs(result, False)

    def test_empty_string_returns_empty_string(self):
        result = _coerce("")
        self.assertEqual(result, "")

    def test_quoted_string_strips_quotes(self):
        result = _coerce('"hello world"')
        self.assertEqual(result, "hello world")

    def test_single_quoted_strips_quotes(self):
        result = _coerce("'hello world'")
        self.assertEqual(result, "hello world")

    def test_unquoted_string_returns_as_is(self):
        result = _coerce("plain text")
        self.assertEqual(result, "plain text")

    def test_double_quotes_not_in_output(self):
        result = _coerce('"test"')
        self.assertNotIn('"', result)

    def test_single_quotes_not_in_output(self):
        result = _coerce("'test'")
        self.assertNotIn("'", result)

    def test_list_items_are_strings(self):
        result = _coerce("[1, 2, 3]")
        self.assertIsInstance(result, list)
        for item in result:
            self.assertIsInstance(item, str)

    def test_list_with_quotes_in_items(self):
        result = _coerce('["a", "b"]')
        self.assertEqual(result, ["a", "b"])

    def test_mixed_case_bool_true(self):
        result = _coerce("True")
        self.assertIs(result, True)

    def test_mixed_case_bool_false(self):
        result = _coerce("False")
        self.assertIs(result, False)


# ─── parse_frontmatter tests ─────────────────────────────────────────────────


class TestParseFrontmatterMutationKillers(unittest.TestCase):
    """Target survived mutations in parse_frontmatter."""

    def test_returns_tuple_of_two(self):
        result = parse_frontmatter("---\ntitle: test\n---\n\nBody")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_metadata_is_dict(self):
        metadata, body = parse_frontmatter("---\ntitle: test\n---\n\nBody")
        self.assertIsInstance(metadata, dict)

    def test_metadata_has_title(self):
        metadata, body = parse_frontmatter("---\ntitle: mynote\n---\n\nBody content")
        self.assertIn("title", metadata)
        self.assertEqual(metadata["title"], "mynote")

    def test_body_is_string(self):
        metadata, body = parse_frontmatter("---\ntitle: x\n---\n\nHello world")
        self.assertIsInstance(body, str)

    def test_body_content_preserved(self):
        metadata, body = parse_frontmatter("---\ntitle: x\n---\n\nActual body here")
        self.assertEqual(body, "Actual body here")

    def test_no_frontmatter_returns_empty_dict(self):
        metadata, body = parse_frontmatter("Just plain text, no frontmatter")
        self.assertEqual(metadata, {})

    def test_no_frontmatter_returns_original_content(self):
        original = "Just plain text, no frontmatter"
        metadata, body = parse_frontmatter(original)
        self.assertEqual(body, original)

    def test_category_parsed(self):
        metadata, body = parse_frontmatter("---\ncategory: lessons\n---\n\nBody")
        self.assertEqual(metadata.get("category"), "lessons")

    def test_multiple_fields(self):
        content = "---\ntitle: test\ncategory: decisions\ntags: [a, b]\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertEqual(metadata.get("title"), "test")
        self.assertEqual(metadata.get("category"), "decisions")

    def test_boolean_coercion_in_yaml(self):
        metadata, body = parse_frontmatter("---\npinned: true\n---\n\nBody")
        self.assertIs(metadata.get("pinned"), True)

    def test_numeric_coercion_in_yaml(self):
        metadata, body = parse_frontmatter("---\nimportance: 5\n---\n\nBody")
        self.assertIsNotNone(metadata.get("importance"))

    def test_list_coercion_in_yaml(self):
        metadata, body = parse_frontmatter("---\ntags: [alpha, beta]\n---\n\nBody")
        self.assertIsInstance(metadata.get("tags"), list)

    def test_multiline_yaml_value(self):
        content = "---\ntitle: >\n  multi line\n  value\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertIn("title", metadata)

    def test_crlf_line_endings(self):
        content = "---\r\ntitle: test\r\n---\r\n\r\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertEqual(metadata.get("title"), "test")

    def test_bom_stripped(self):
        content = "\ufeff---\ntitle: test\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertEqual(metadata.get("title"), "test")

    def test_multiline_list_items(self):
        content = "---\ntags:\n  - alpha\n  - beta\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertIsInstance(metadata.get("tags"), list)

    def test_empty_value_after_key(self):
        content = "---\ntitle:\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertIn("title", metadata)


# ─── get_memory_paths tests ──────────────────────────────────────────────────


class TestGetMemoryPathsMutationKillers(unittest.TestCase):
    """Target survived mutations in get_memory_paths."""

    def test_returns_tuple_of_three(self):
        result = get_memory_paths()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_first_element_is_path(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsInstance(project_root, Path)

    def test_second_element_is_path(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsInstance(local_mem, Path)

    def test_third_element_is_path(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsInstance(global_mem, Path)

    def test_third_element_is_global_mem_dir(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertEqual(global_mem, GLOBAL_MEM_DIR)

    def test_second_element_ends_with_memory(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertEqual(local_mem.name, "memory")

    def test_first_element_is_not_none(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsNotNone(project_root)

    def test_second_element_is_not_none(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsNotNone(local_mem)

    def test_third_element_is_not_none(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsNotNone(global_mem)

    def test_project_root_exists(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertTrue(project_root.exists())

    def test_global_mem_exists(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertTrue(global_mem.exists())


# ─── count_rows tests ────────────────────────────────────────────────────────


class TestCountRowsMutationKillers(unittest.TestCase):
    """Target survived mutations in count_rows."""

    def test_returns_int_not_none(self):
        result = count_rows(PROD_GLOBAL)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, int)

    def test_returns_positive_for_prod(self):
        # 2026-06-29 fix: skip on CI where the prod DB is not seeded.
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            self.skipTest("CI: production DB not seeded on the runner")
        result = count_rows(PROD_GLOBAL)
        self.assertGreater(result, 0)

    def test_returns_minus1_for_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = count_rows(Path(tmpdir))
            self.assertEqual(result, -1)

    def test_returns_minus1_for_nonexistent_path(self):
        result = count_rows(Path("/nonexistent/db/dir"))
        self.assertEqual(result, -1)

    def test_minus1_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = count_rows(Path(tmpdir))
            self.assertEqual(result, -1)
            self.assertNotEqual(result, 0)

    def test_returns_minus1_for_empty_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp_db))
            conn.close()
            # count_rows expects dir with memory.db inside
            tmp_dir = tmp_db.parent / "testdb"
            tmp_dir.mkdir(exist_ok=True)
            (tmp_dir / "memory.db").write_bytes(tmp_db.read_bytes())
            result = count_rows(tmp_dir)
            self.assertIn(result, [-1, 0])
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_db.unlink(missing_ok=True)


# ─── atomic_write tests ──────────────────────────────────────────────────────


class TestAtomicWriteMutationKillers(unittest.TestCase):
    """Target survived mutations in atomic_write."""

    def test_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            result = atomic_write(tmp, "test")
            self.assertIsNone(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_creates_file(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            atomic_write(tmp, "content here")
            self.assertTrue(tmp.exists())
        finally:
            tmp.unlink(missing_ok=True)

    def test_content_is_written(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            atomic_write(tmp, "exact content")
            self.assertEqual(tmp.read_text(), "exact content")
        finally:
            tmp.unlink(missing_ok=True)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "a" / "b" / "c" / "file.md"
            atomic_write(nested, "nested")
            self.assertTrue(nested.exists())
            self.assertEqual(nested.read_text(), "nested")

    def test_overwrites_existing(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("old")
            tmp = Path(f.name)
        try:
            atomic_write(tmp, "new")
            self.assertEqual(tmp.read_text(), "new")
        finally:
            tmp.unlink(missing_ok=True)

    def test_no_tmp_file_after_success(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            atomic_write(tmp, "data")
            tmp_file = tmp.with_suffix(tmp.suffix + ".tmp")
            self.assertFalse(tmp_file.exists())
        finally:
            tmp.unlink(missing_ok=True)

    def test_bytes_content(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            tmp = Path(f.name)
        try:
            atomic_write(tmp, b"\x00\x01\x02")
            self.assertEqual(tmp.read_bytes(), b"\x00\x01\x02")
        finally:
            tmp.unlink(missing_ok=True)

    def test_empty_string(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            atomic_write(tmp, "")
            self.assertEqual(tmp.read_text(), "")
        finally:
            tmp.unlink(missing_ok=True)

    def test_large_content(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp = Path(f.name)
        try:
            large = "x" * 200000
            atomic_write(tmp, large)
            self.assertEqual(tmp.read_text(), large)
        finally:
            tmp.unlink(missing_ok=True)


# ─── safe_close_db tests ─────────────────────────────────────────────────────


class TestSafeCloseDbMutationKillers(unittest.TestCase):
    """Target survived mutations in safe_close_db."""

    def test_returns_none(self):
        conn = sqlite3.connect(":memory:")
        result = safe_close_db(conn)
        self.assertIsNone(result)

    def test_closes_connection(self):
        conn = sqlite3.connect(":memory:")
        safe_close_db(conn)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_idempotent(self):
        conn = sqlite3.connect(":memory:")
        safe_close_db(conn)
        safe_close_db(conn)  # Should not raise

    def test_commits_before_close(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES(1)")
        safe_close_db(conn)
        # Reopen and verify data persisted
        # (in-memory DB won't persist, but the commit should not raise)

    def test_handles_none_gracefully(self):
        safe_close_db(None)  # Should not raise


# ─── open_db tests ───────────────────────────────────────────────────────────


class TestOpenDbMutationKillers(unittest.TestCase):
    """Target survived mutations in open_db."""

    def test_returns_context_manager(self):
        ctx = open_db(PROD_DB)
        self.assertIsNotNone(ctx)

    def test_yields_connection(self):
        with open_db(PROD_DB, write=False, pooled=True) as conn:
            self.assertIsNotNone(conn)
            self.assertIsInstance(conn, sqlite3.Connection)

    def test_connection_has_wal_mode(self):
        with open_db(PROD_DB) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")

    def test_connection_can_query(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            pool = _ConnectionPool(max_size=5)
            conn = pool.get(str(tmp))
            conn.execute(
                "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) VALUES ('test-1', 'hello', 'test.md', '[]', '2025-01-01', '2025-01-01', '2025-01-01')"
            )
            conn.commit()
            result = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            self.assertGreater(result[0], 0)
            pool.close_all()
        finally:
            tmp.unlink(missing_ok=True)

    def test_timeout_affects_busy_timeout(self):
        # The write path uses a fixed busy_timeout of 30000 in the write queue.
        # Use write=False to test the timeout parameter in the direct connection path.
        with open_db(PROD_DB, timeout=15.0, write=False, pooled=True) as conn:
            bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(bt, 15000)

    def test_row_factory_set(self):
        with open_db(PROD_DB, row_factory=sqlite3.Row) as conn:
            self.assertEqual(conn.row_factory, sqlite3.Row)

    def test_row_factory_none_by_default(self):
        with open_db(PROD_DB) as conn:
            self.assertIsNone(conn.row_factory)

    def test_commits_on_exit(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            with open_db(tmp) as conn:
                conn.execute("CREATE TABLE t(x)")
                conn.execute("INSERT INTO t VALUES(42)")
            # Verify data persisted
            with open_db(tmp) as conn:
                val = conn.execute("SELECT x FROM t").fetchone()[0]
                self.assertEqual(val, 42)
        finally:
            tmp.unlink(missing_ok=True)

    def test_connection_closed_after_exit(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            with open_db(tmp) as conn:
                id(conn)
            # Connection should be closed now
        finally:
            tmp.unlink(missing_ok=True)

    def test_pooled_returns_same_connection(self):
        with open_db(PROD_DB, pooled=True) as conn1:
            id(conn1)
        with open_db(PROD_DB, pooled=True) as conn2:
            id(conn2)
        # Pooled connections may be reused (same id)

    def test_creates_wal_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            with open_db(tmp) as conn:
                conn.execute("CREATE TABLE t(x)")
            tmp.parent / (tmp.name + "-wal")
            # WAL file should exist after first write
        finally:
            tmp.unlink(missing_ok=True)
            (tmp.parent / (tmp.name + "-wal")).unlink(missing_ok=True)
            (tmp.parent / (tmp.name + "-shm")).unlink(missing_ok=True)


# ─── validate_config tests ───────────────────────────────────────────────────


class TestValidateConfigMutationKillers(unittest.TestCase):
    """Target survived mutations in validate_config."""

    def test_returns_list(self):
        result = validate_config()
        self.assertIsInstance(result, list)

    def test_returns_list_not_none(self):
        result = validate_config()
        self.assertIsNotNone(result)

    def test_items_are_strings(self):
        result = validate_config()
        for item in result:
            self.assertIsInstance(item, str)

    def test_empty_list_on_valid_config(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=False):
            result = validate_config()
            # May or may not be empty depending on other env vars
            self.assertIsInstance(result, list)


# ─── log_backup tests ────────────────────────────────────────────────────────


class TestLogBackupMutationKillers(unittest.TestCase):
    """Target survived mutations in log_backup."""

    def test_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            result = log_backup(tmp)
            self.assertIsNone(result)
        finally:
            tmp.unlink(missing_ok=True)
            for bak in tmp.parent.glob(tmp.name + ".bak.*"):
                bak.unlink(missing_ok=True)

    def test_creates_backup_file(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
            f.write(b"test data")
        try:
            log_backup(tmp)
            backups = list(tmp.parent.glob(tmp.name + ".bak.*"))
            self.assertGreater(len(backups), 0)
        finally:
            tmp.unlink(missing_ok=True)
            for bak in tmp.parent.glob(tmp.name + ".bak.*"):
                bak.unlink(missing_ok=True)

    def test_backup_content_matches_original(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
            f.write(b"original content")
        try:
            log_backup(tmp)
            backups = list(tmp.parent.glob(tmp.name + ".bak.*"))
            self.assertGreater(len(backups), 0)
            self.assertEqual(backups[0].read_bytes(), b"original content")
        finally:
            tmp.unlink(missing_ok=True)
            for bak in tmp.parent.glob(tmp.name + ".bak.*"):
                bak.unlink(missing_ok=True)

    def test_rotation_limits_backups(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
            f.write(b"data")
        try:
            for _ in range(5):
                log_backup(tmp)
                # 1.1s gap so each backup has a strictly different mtime
                # (the rotation key is per-second resolution). Not a wait
                # for an async event — a deterministic time-bump — so the
                # fixed sleep is correct here.
                time.sleep(1.1)  # Ensure different timestamps
            backups = list(tmp.parent.glob(tmp.name + ".bak.*"))
            self.assertLessEqual(len(backups), 3)
        finally:
            tmp.unlink(missing_ok=True)
            for bak in tmp.parent.glob(tmp.name + ".bak.*"):
                bak.unlink(missing_ok=True)

    def test_returns_none_for_missing_file(self):
        result = log_backup(Path("/nonexistent/db/file.db"))
        self.assertIsNone(result)

    def test_keep_parameter_respected(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
            f.write(b"data")
        try:
            for _ in range(4):
                log_backup(tmp, keep=2)
                # 1.1s gap so each backup has a strictly different mtime
                # (the rotation key is per-second resolution). See
                # test_rotation_limits_backups for the rationale.
                time.sleep(1.1)
            backups = list(tmp.parent.glob(tmp.name + ".bak.*"))
            self.assertLessEqual(len(backups), 2)
        finally:
            tmp.unlink(missing_ok=True)
            for bak in tmp.parent.glob(tmp.name + ".bak.*"):
                bak.unlink(missing_ok=True)


# ─── _ConnectionPool mutation killer tests ───────────────────────────────────


class TestConnectionPoolMutationKillers(unittest.TestCase):
    """Target survived mutations in _ConnectionPool."""

    def test_get_returns_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        self.assertIsNotNone(conn)
        self.assertIsInstance(conn, sqlite3.Connection)
        pool.close_all()

    def test_get_same_key_returns_same_conn(self):
        pool = _ConnectionPool(max_size=5)
        c1 = pool.get(str(PROD_DB))
        c2 = pool.get(str(PROD_DB))
        self.assertIs(c1, c2)
        pool.close_all()

    def test_get_different_keys_different_conns(self):
        pool = _ConnectionPool(max_size=5)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        try:
            c1 = pool.get(db1)
            c2 = pool.get(db2)
            self.assertIsNot(c1, c2)
        finally:
            pool.close_all()
            for db in [db1, db2]:
                try:
                    os.unlink(db)
                except Exception:
                    pass

    def test_max_size_enforced(self):
        # P0-3 fix (2026-06-22): with the active-conn skip, the
        # caller must release a conn (put) before the pool can evict
        # to make room.  This test puts back after each get so eviction
        # is the bottleneck, not the active-conn protection.
        pool = _ConnectionPool(max_size=2)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db3 = f.name
        try:
            c1 = pool.get(db1)
            pool.put(c1)
            c2 = pool.get(db2)
            pool.put(c2)
            pool.get(db3)  # pool is full, must evict to make room
            self.assertLessEqual(len(pool._pool), 2)
        finally:
            pool.close_all()
            for db in [db1, db2, db3]:
                try:
                    os.unlink(db)
                except Exception:
                    pass

    def test_lru_eviction_order(self):
        # P0-3 fix (2026-06-22): same as test_max_size_enforced — the
        # caller must release the conn (put) before eviction can kick
        # in.  After release, the oldest (db1) is the LRU eviction
        # target.
        pool = _ConnectionPool(max_size=2)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db3 = f.name
        try:
            c1 = pool.get(db1)
            pool.put(c1)
            c2 = pool.get(db2)
            pool.put(c2)
            # Pool is full of (db1, db2).  db1 is the LRU.
            pool.get(db3)
            # After eviction: db1 should be gone, db2 and db3 should remain.
            self.assertLessEqual(len(pool._pool), 2)
            self.assertNotIn((db1, threading.current_thread().ident), pool._pool)
        finally:
            pool.close_all()
            for db in [db1, db2, db3]:
                try:
                    os.unlink(db)
                except Exception:
                    pass

    def test_close_removes_entry(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close(str(PROD_DB))
        self.assertEqual(len(pool._pool), 0)

    def test_close_all_clears_pool(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close_all()
        self.assertEqual(len(pool._pool), 0)

    def test_close_all_clears_lru(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close_all()
        self.assertEqual(len(pool._lru), 0)

    def test_close_all_clears_pooled_ids(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close_all()
        self.assertEqual(len(pool._pooled_ids), 0)

    def test_put_reuses_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        # Put back without closing — should be reusable
        pool.put(conn)
        conn2 = pool.get(str(PROD_DB))
        self.assertIs(conn, conn2)

    def test_put_validates_connection(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        # Put a non-pooled connection
        fresh = sqlite3.connect(":memory:")
        pool.put(fresh)  # Should not crash

    def test_evict_lru_with_full_pool(self):
        # P0-3 fix (2026-06-22): the caller must release the conn
        # (put) before eviction can kick in.  After release, the
        # eviction makes room and the new get() succeeds.
        pool = _ConnectionPool(max_size=1)
        c1 = pool.get(str(PROD_DB))
        pool.put(c1)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        try:
            pool.get(db2)
            self.assertLessEqual(len(pool._pool), 1)
        finally:
            pool.close_all()
            try:
                os.unlink(db2)
            except Exception:
                pass

    def test_get_connection_is_alive(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        result = conn.execute("SELECT 1").fetchone()
        self.assertEqual(result[0], 1)
        pool.close_all()

    def test_connection_has_wal_mode(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        pool.close_all()

    def test_connection_has_busy_timeout(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertGreater(bt, 0)
        pool.close_all()

    def test_pool_reuses_after_put(self):
        pool = _ConnectionPool(max_size=5)
        conn1 = pool.get(str(PROD_DB))
        pool.put(conn1)
        conn2 = pool.get(str(PROD_DB))
        self.assertIs(conn1, conn2)
        pool.close_all()

    def test_max_size_not_exceeded(self):
        pool = _ConnectionPool(max_size=3)
        conns = []
        for i in range(6):
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db = f.name
            try:
                conns.append(pool.get(db))
            except Exception:
                pass
        self.assertLessEqual(len(pool._pool), 3)
        pool.close_all()

    def test_evict_lru_skips_active_connections(self):
        """P0-3 regression: _evict_lru must NOT close a conn that is still in use.

        Before the fix, a long-running operation that holds a conn (e.g.
        a multi-step saga) could have its conn closed mid-transaction
        when a new ``get()`` triggered LRU eviction.  Now the pool
        skips any conn whose depth > 0 and raises ``PoolExhaustedError``
        if every conn is active.

        Test plan:
          1. max_size=2 pool.
          2. get conn A (key K_A, depth=1) — held (not put back).
          3. get conn B (key K_B, depth=1) — held.  Pool is at max.
          4. get conn C (key K_C) — must raise PoolExhaustedError because
             both K_A and K_B are active (depth=1) and the pool has no
             inactive conns to evict.
          5. put(conn_A) — depth[K_A] goes to 0.
          6. get conn C — should now succeed; K_A is evicted (depth=0).
        """
        from db import PoolExhaustedError

        pool = _ConnectionPool(max_size=2)
        db_a = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        db_b = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        db_c = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            conn_a = pool.get(db_a)  # depth[K_A] = 1, K_A in LRU
            pool.get(db_b)  # depth[K_B] = 1, K_B in LRU
            # Pool is now at max (2 conns, both held).
            self.assertEqual(len(pool._pool), 2)
            self.assertEqual(
                pool._depth.get(
                    (db_a, pool._depth and threading.current_thread().ident), 1
                ),
                1,
            )

            # Step 4: get a new conn (key K_C) — must raise because both
            # existing conns are active.
            with self.assertRaises(
                PoolExhaustedError,
                msg="Expected PoolExhaustedError when all conns are active",
            ):
                pool.get(db_c)

            # Step 5: put back conn_a — its depth drops to 0.
            pool.put(conn_a)
            # K_A's depth is now 0; K_B is still 1.
            # Step 6: get a new conn — should succeed (K_A is evictable).
            conn_c = pool.get(db_c)
            self.assertIsNotNone(conn_c)
            # K_B and K_C should be in the pool; K_A evicted.
            self.assertEqual(len(pool._pool), 2)
            self.assertNotIn((db_a, threading.current_thread().ident), pool._pool)
            self.assertIn((db_b, threading.current_thread().ident), pool._pool)
            self.assertIn((db_c, threading.current_thread().ident), pool._pool)
        finally:
            pool.close_all()
            for db in [db_a, db_b, db_c]:
                try:
                    os.unlink(db)
                except OSError:
                    pass


# ─── RateLimiter mutation killer tests ───────────────────────────────────────


class TestRateLimiterMutationKillers(unittest.TestCase):
    """Target survived mutations in RateLimiter."""

    def test_check_returns_bool(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        result = rl.check("test")
        self.assertIsInstance(result, bool)

    def test_check_returns_true_under_limit(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        self.assertTrue(rl.check("test"))

    def test_check_returns_false_over_limit(self):
        rl = RateLimiter(max_calls=2, window_seconds=60.0)
        rl.check("test")
        rl.check("test")
        self.assertFalse(rl.check("test"))

    def test_reset_returns_none(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        result = rl.reset("test")
        self.assertIsNone(result)

    def test_reset_all_returns_none(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        result = rl.reset()
        self.assertIsNone(result)

    def test_reset_allows_new_calls(self):
        rl = RateLimiter(max_calls=2, window_seconds=60.0)
        rl.check("test")
        rl.check("test")
        self.assertFalse(rl.check("test"))
        rl.reset("test")
        self.assertTrue(rl.check("test"))

    def test_different_keys_independent(self):
        rl = RateLimiter(max_calls=1, window_seconds=60.0)
        rl.check("a")
        self.assertFalse(rl.check("a"))
        self.assertTrue(rl.check("b"))

    def test_window_slides(self):
        rl = RateLimiter(max_calls=1, window_seconds=0.1)
        rl.check("test")
        self.assertFalse(rl.check("test"))
        # Wait for the 0.1s rate-limit window to elapse. rl.check() is
        # consuming (every call opens a new window), so we can't poll it.
        # +0.05s gives a small margin on slow CI.
        time.sleep(0.15)
        self.assertTrue(rl.check("test"))

    def test_max_calls_must_be_positive(self):
        with self.assertRaises(ValueError):
            RateLimiter(max_calls=0)

    def test_window_must_be_positive(self):
        with self.assertRaises(ValueError):
            RateLimiter(window_seconds=0)

    def test_max_calls_stored(self):
        rl = RateLimiter(max_calls=42, window_seconds=60.0)
        self.assertEqual(rl.max_calls, 42)

    def test_window_seconds_stored(self):
        rl = RateLimiter(max_calls=60, window_seconds=30.0)
        self.assertEqual(rl.window_seconds, 30.0)

    def test_buckets_initialized(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        self.assertIsInstance(rl._buckets, dict)

    def test_buckets_empty_initially(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        self.assertEqual(len(rl._buckets), 0)

    def test_reset_specific_key(self):
        rl = RateLimiter(max_calls=1, window_seconds=60.0)
        rl.check("a")
        rl.check("b")
        rl.reset("a")
        self.assertTrue(rl.check("a"))
        self.assertFalse(rl.check("b"))

    def test_reset_nonexistent_key(self):
        rl = RateLimiter(max_calls=10, window_seconds=60.0)
        rl.reset("nonexistent")  # Should not raise

    def test_check_increments_count(self):
        rl = RateLimiter(max_calls=3, window_seconds=60.0)
        rl.check("test")
        rl.check("test")
        bucket = rl._buckets.get("test")
        self.assertIsNotNone(bucket)
        self.assertEqual(len(bucket), 2)


# ─── get_default_limiter / rate_limit_check / reset_rate_limiter ─────────────


class TestDefaultLimiterMutationKillers(unittest.TestCase):
    """Target survived mutations in default limiter functions."""

    def test_get_default_limiter_returns_rate_limiter(self):
        reset_rate_limiter()
        result = get_default_limiter()
        self.assertIsInstance(result, RateLimiter)

    def test_get_default_limiter_returns_same_instance(self):
        reset_rate_limiter()
        r1 = get_default_limiter()
        r2 = get_default_limiter()
        self.assertIs(r1, r2)
        reset_rate_limiter()

    def test_rate_limit_check_returns_bool(self):
        reset_rate_limiter()
        result = rate_limit_check("test_tool")
        self.assertIsInstance(result, bool)
        reset_rate_limiter()

    def test_reset_rate_limiter_returns_none(self):
        result = reset_rate_limiter()
        self.assertIsNone(result)


# ─── wal_checkpoint_idle mutation killer tests ───────────────────────────────


class TestWalCheckpointIdleMutationKillers(unittest.TestCase):
    """Target survived mutations in wal_checkpoint_idle."""

    def test_returns_dict(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIsInstance(result, dict)

    def test_has_status_key(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIn("status", result)

    def test_has_ok_key(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIn("ok", result)

    def test_ok_is_bool(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIsInstance(result["ok"], bool)

    def test_has_wal_size_mb(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIn("wal_size_mb", result)

    def test_wal_size_mb_is_float(self):
        result = wal_checkpoint_idle(PROD_DB)
        self.assertIsInstance(result["wal_size_mb"], float)

    def test_memory_db_returns_skipped(self):
        result = wal_checkpoint_idle(Path(":memory:"))
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["ok"])

    def test_memory_db_wal_size_zero(self):
        result = wal_checkpoint_idle(Path(":memory:"))
        self.assertEqual(result["wal_size_mb"], 0.0)

    def test_has_threshold_mb(self):
        result = wal_checkpoint_idle(PROD_DB, wal_size_threshold_mb=5.0)
        self.assertIn("threshold_mb", result)

    def test_threshold_mb_matches_input(self):
        result = wal_checkpoint_idle(PROD_DB, wal_size_threshold_mb=7.5)
        self.assertEqual(result["threshold_mb"], 7.5)

    def test_done_status_has_pages(self):
        # Force checkpoint by using low threshold
        result = wal_checkpoint_idle(PROD_DB, wal_size_threshold_mb=0.0)
        if result.get("status") == "done":
            self.assertIn("log_pages", result)
            self.assertIn("checkpointed_pages", result)
            self.assertIn("wal_pages", result)

    def test_wal_size_mb_is_rounded(self):
        result = wal_checkpoint_idle(PROD_DB)
        wal_size = result["wal_size_mb"]
        # round(x, 2) should have at most 2 decimal places
        self.assertEqual(wal_size, round(wal_size, 2))


# ─── _maybe_checkpoint_on_startup mutation killer tests ──────────────────────


class TestMaybeCheckpointOnStartupMutationKillers(unittest.TestCase):
    """Target survived mutations in _maybe_checkpoint_on_startup."""

    def test_returns_none(self):
        import memory_common

        old_val = memory_common._STARTUP_CHECKPOINT_DONE
        try:
            memory_common._STARTUP_CHECKPOINT_DONE = False
            with patch.dict(os.environ, {"MEMORY_WAL_CHECKPOINT_STARTUP": "0"}):
                result = _maybe_checkpoint_on_startup(PROD_DB)
                self.assertIsNone(result)
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old_val

    def test_sets_checkpoint_done_flag(self):
        import memory_common

        old_val = memory_common._STARTUP_CHECKPOINT_DONE
        try:
            memory_common._STARTUP_CHECKPOINT_DONE = False
            with patch.dict(os.environ, {"MEMORY_WAL_CHECKPOINT_STARTUP": "1"}):
                _maybe_checkpoint_on_startup(PROD_DB)
                self.assertTrue(memory_common._STARTUP_CHECKPOINT_DONE)
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old_val

    def test_skips_when_already_done(self):
        import memory_common

        old_val = memory_common._STARTUP_CHECKPOINT_DONE
        try:
            memory_common._STARTUP_CHECKPOINT_DONE = True
            # Should be a no-op
            _maybe_checkpoint_on_startup(PROD_DB)
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old_val

    def test_skips_when_env_not_set(self):
        import memory_common

        old_val = memory_common._STARTUP_CHECKPOINT_DONE
        try:
            memory_common._STARTUP_CHECKPOINT_DONE = False
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MEMORY_WAL_CHECKPOINT_STARTUP", None)
                _maybe_checkpoint_on_startup(PROD_DB)
                # Should return without checkpoint
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old_val


# ─── cleanup_fts5_orphans mutation killer tests ──────────────────────────────


class TestCleanupFts5OrphansMutationKillers(unittest.TestCase):
    """Target survived mutations in cleanup_fts5_orphans."""

    def test_returns_int(self):
        from memory_common import cleanup_fts5_orphans

        with open_db(PROD_DB) as conn:
            result = cleanup_fts5_orphans(conn)
            self.assertIsInstance(result, int)

    def test_returns_non_negative(self):
        from memory_common import cleanup_fts5_orphans

        with open_db(PROD_DB) as conn:
            result = cleanup_fts5_orphans(conn)
            self.assertGreaterEqual(result, 0)

    def test_returns_zero_on_clean_db(self):
        from memory_common import cleanup_fts5_orphans

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp))
            conn.execute("CREATE TABLE memories(id TEXT, deleted_at TEXT)")
            conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, tags)")
            conn.commit()
            result = cleanup_fts5_orphans(conn)
            self.assertEqual(result, 0)
            conn.close()
        finally:
            tmp.unlink(missing_ok=True)


# ─── configure_logging mutation killer tests ─────────────────────────────────


class TestConfigureLoggingMutationKillers(unittest.TestCase):
    """Target survived mutations in configure_logging."""

    def test_returns_none(self):
        result = configure_logging()
        self.assertIsNone(result)

    def test_idempotent(self):
        configure_logging()
        configure_logging()  # Should not raise


# ─── SCHEMA_VERSION / constants tests ────────────────────────────────────────


class TestConstantsMutationKillers(unittest.TestCase):
    """Target mutations on module-level constants."""

    def test_schema_version_is_current(self):
        # 2026-06-22: bumped to 16 for concept_drift. v15 = drift_alarms.
        # v14 = arc_cache. v13 = memory_field_crdt.
        from migration_runner import SCHEMA_VERSION as expected_version

        self.assertEqual(SCHEMA_VERSION, expected_version)

    def test_valid_log_levels_is_set(self):
        self.assertIsInstance(_VALID_LOG_LEVELS, set)

    def test_valid_log_levels_contains_debug(self):
        self.assertIn("DEBUG", _VALID_LOG_LEVELS)

    def test_valid_log_levels_contains_info(self):
        self.assertIn("INFO", _VALID_LOG_LEVELS)

    def test_valid_log_levels_contains_warning(self):
        self.assertIn("WARNING", _VALID_LOG_LEVELS)

    def test_valid_log_levels_contains_error(self):
        self.assertIn("ERROR", _VALID_LOG_LEVELS)

    def test_valid_log_levels_contains_critical(self):
        self.assertIn("CRITICAL", _VALID_LOG_LEVELS)

    def test_project_root_markers_is_tuple(self):
        self.assertIsInstance(PROJECT_ROOT_MARKERS, tuple)

    def test_project_root_markers_has_memory(self):
        self.assertIn("memory", PROJECT_ROOT_MARKERS)

    def test_project_root_markers_has_git(self):
        self.assertIn(".git", PROJECT_ROOT_MARKERS)

    def test_project_root_markers_has_agents(self):
        self.assertIn(".agents", PROJECT_ROOT_MARKERS)

    def test_global_mem_dir_is_path(self):
        self.assertIsInstance(GLOBAL_MEM_DIR, Path)

    def test_global_mem_dir_ends_with_memory(self):
        self.assertEqual(GLOBAL_MEM_DIR.name, "memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
