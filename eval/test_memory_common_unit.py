#!/usr/bin/env python3
"""Unit tests for memory_common.py — targeted at mutation survival sites.

Covers:
- _ConnectionPool: LRU eviction, max_size enforcement, get/put/close
- count_rows: return values, error handling, missing DB
- safe_call: exception handling, fallback values
- atomic_write: file integrity, temp file cleanup
- open_db: connection creation, WAL mode
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import (
    _ConnectionPool,
    count_rows,
    safe_call,
    atomic_write,
    open_db,
    safe_close_db,
)
from infrastructure import GLOBAL_MEM_DIR

PROD_DB = Path(os.environ.get("MEMORY_DB_PATH", str(GLOBAL_MEM_DIR / "memory.db")))


class TestConnectionPool(unittest.TestCase):
    """Test _ConnectionPool LRU eviction and max_size enforcement."""

    def test_pool_creates_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        self.assertIsNotNone(conn)
        self.assertIsInstance(conn, sqlite3.Connection)
        pool.close(str(PROD_DB))

    def test_pool_reuses_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn1 = pool.get(str(PROD_DB))
        conn2 = pool.get(str(PROD_DB))
        self.assertIs(conn1, conn2)
        pool.close_all()

    def test_pool_evicts_lru_at_capacity(self):
        pool = _ConnectionPool(max_size=2)
        # Create 3 connections (capacity is 2)
        db_path = str(PROD_DB)
        pool.get(db_path)
        # Use a temp DB for the second connection
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name
        try:
            pool.get(tmp_db)
            pool.get(db_path)  # Should evict conn2 (LRU)
            # Pool should have at most 2 connections
            self.assertLessEqual(len(pool._pool), 2)
        finally:
            pool.close_all()
            os.unlink(tmp_db)

    def test_pool_max_size_enforced(self):
        pool = _ConnectionPool(max_size=3)
        conns = []
        for i in range(5):
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                tmp_db = f.name
            try:
                conn = pool.get(tmp_db)
                conns.append((tmp_db, conn))
            except Exception:
                pass
        # Pool should not exceed max_size
        self.assertLessEqual(len(pool._pool), 3)
        pool.close_all()
        for tmp_db, _ in conns:
            try:
                os.unlink(tmp_db)
            except Exception:
                pass

    def test_pool_close_removes_connection(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close(str(PROD_DB))
        self.assertNotIn(str(PROD_DB), pool._pool)

    def test_pool_close_all(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name
        try:
            pool.get(tmp_db)
            pool.close_all()
            self.assertEqual(len(pool._pool), 0)
        finally:
            try:
                os.unlink(tmp_db)
            except Exception:
                pass

    def test_pool_lru_eviction_maintains_max_size(self):
        """Verify pool doesn't exceed max_size even with many connections."""
        pool = _ConnectionPool(max_size=2)
        dbs = []
        for i in range(4):
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                tmp_db = f.name
            try:
                pool.get(tmp_db)
                dbs.append(tmp_db)
            except Exception:
                pass
        # Pool should have at most 2 connections
        self.assertLessEqual(len(pool._pool), 2)
        pool.close_all()
        for db in dbs:
            try:
                os.unlink(db)
            except Exception:
                pass


class TestCountRows(unittest.TestCase):
    """Test count_rows return values and error handling."""

    def test_returns_positive_for_prod_db(self):
        count = count_rows(GLOBAL_MEM_DIR)
        self.assertGreater(count, 0)

    def test_returns_minus1_for_missing_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            count = count_rows(Path(tmpdir))
            self.assertEqual(count, -1)

    def test_returns_int(self):
        count = count_rows(GLOBAL_MEM_DIR)
        self.assertIsInstance(count, int)


class TestSafeCall(unittest.TestCase):
    """Test safe_call exception handling and fallback."""

    def test_returns_result_on_success(self):
        result = safe_call(lambda x: x + 1, 5)
        self.assertEqual(result, 6)

    def test_returns_fallback_on_exception(self):
        result = safe_call(lambda: 1 / 0, fallback="error")
        self.assertEqual(result, "error")

    def test_returns_none_by_default(self):
        result = safe_call(lambda: 1 / 0)
        self.assertIsNone(result)

    def test_passes_args_and_kwargs(self):
        result = safe_call(lambda a, b=0: a + b, 3, b=7)
        self.assertEqual(result, 10)

    def test_err_label_in_log(self):
        with self.assertLogs(level="WARNING") as cm:
            result = safe_call(lambda: 1 / 0, fallback="err", err_label="test_op")
            self.assertEqual(result, "err")
            self.assertTrue(any("test_op" in msg for msg in cm.output))


class TestAtomicWrite(unittest.TestCase):
    """Test atomic_write file integrity."""

    def test_writes_string_content(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, "Hello, world!")
            self.assertTrue(tmp_path.exists())
            self.assertEqual(tmp_path.read_text(), "Hello, world!")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_writes_bytes_content(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, b"Binary content")
            self.assertTrue(tmp_path.exists())
            self.assertEqual(tmp_path.read_bytes(), b"Binary content")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "subdir" / "nested" / "test.md"
            atomic_write(nested, "Nested content")
            self.assertTrue(nested.exists())
            self.assertEqual(nested.read_text(), "Nested content")

    def test_overwrites_existing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("Original")
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, "Overwritten")
            self.assertEqual(tmp_path.read_text(), "Overwritten")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_no_temp_file_left_on_success(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, "Content")
            tmp_file = tmp_path.with_suffix(tmp_path.suffix + ".tmp")
            self.assertFalse(tmp_file.exists())
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_no_temp_file_left_on_failure(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            # Force an error by passing invalid content type
            with self.assertRaises(TypeError):
                atomic_write(tmp_path, 12345)  # int is not str or bytes
            tmp_file = tmp_path.with_suffix(tmp_path.suffix + ".tmp")
            self.assertFalse(tmp_file.exists())
        finally:
            tmp_path.unlink(missing_ok=True)


class TestOpenDb(unittest.TestCase):
    """Test open_db connection creation and WAL mode."""

    def test_returns_context_manager(self):
        ctx = open_db(PROD_DB)
        self.assertIsNotNone(ctx)

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


class TestSafeCloseDb(unittest.TestCase):
    """Test safe_close_db handles various states."""

    def test_closes_open_connection(self):
        conn = sqlite3.connect(":memory:")
        safe_close_db(conn)
        # Should not raise

    def test_idempotent_on_closed(self):
        conn = sqlite3.connect(":memory:")
        safe_close_db(conn)
        safe_close_db(conn)  # Should not raise


class TestConnectionPoolMore(unittest.TestCase):
    """Additional tests for _ConnectionPool."""

    def test_pool_reuses_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn1 = pool.get(str(PROD_DB))
        conn2 = pool.get(str(PROD_DB))
        # Should be the same connection object
        self.assertIs(conn1, conn2)
        pool.close_all()

    def test_pool_max_size_enforced(self):
        pool = _ConnectionPool(max_size=2)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db3 = f.name
        try:
            # Get two connections
            c1 = pool.get(db1)
            pool.get(db2)
            self.assertEqual(len(pool._pool), 2)
            # Release one so the LRU eviction can work (depth goes to 0)
            pool.put(c1)
            # Now getting db3 should evict the LRU (db1) entry
            c3 = pool.get(db3)
            self.assertIsNotNone(c3)
            self.assertLessEqual(len(pool._pool), 2)
        finally:
            pool.close_all()
            for db in [db1, db2, db3]:
                try:
                    os.unlink(db)
                except Exception:
                    pass

    def test_pool_close_removes_entry(self):
        pool = _ConnectionPool(max_size=5)
        pool.get(str(PROD_DB))
        pool.close(str(PROD_DB))
        self.assertEqual(len(pool._pool), 0)


class TestCountRowsMore(unittest.TestCase):
    """Additional tests for count_rows."""

    def test_count_rows_with_nonexistent_table(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = Path(f.name)
        try:
            # Create empty DB
            conn = sqlite3.connect(str(tmp_db))
            conn.close()
            count = count_rows(tmp_db)
            # Should return -1 or 0 for missing table
            self.assertIn(count, [-1, 0])
        finally:
            tmp_db.unlink(missing_ok=True)


class TestSafeCallMore(unittest.TestCase):
    """Additional tests for safe_call."""

    def test_returns_default_on_exception(self):
        result = safe_call(lambda: 1 / 0, fallback=42)
        self.assertEqual(result, 42)

    def test_no_fallback_returns_none(self):
        result = safe_call(lambda: 1 / 0)
        self.assertIsNone(result)


class TestAtomicWriteMore(unittest.TestCase):
    """Additional tests for atomic_write."""

    def test_writes_empty_string(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, "")
            self.assertTrue(tmp_path.exists())
            self.assertEqual(tmp_path.read_text(), "")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_writes_large_content(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            large_content = "x" * 100000
            atomic_write(tmp_path, large_content)
            self.assertEqual(tmp_path.read_text(), large_content)
        finally:
            tmp_path.unlink(missing_ok=True)


class TestOpenDbMore(unittest.TestCase):
    """Additional tests for open_db."""

    def test_context_manager_yields_connection(self):
        # Use write=False to get a direct sqlite3.Connection for read-only tests
        with open_db(PROD_DB, write=False, pooled=True) as conn:
            self.assertIsNotNone(conn)
            self.assertIsInstance(conn, sqlite3.Connection)


class TestSafeCloseDbMore(unittest.TestCase):
    """Additional tests for safe_close_db."""

    def test_handles_none(self):
        # Should not raise
        safe_close_db(None)

    def test_handles_already_closed(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        # Should not raise
        safe_close_db(conn)


class TestConnectionPoolGet(unittest.TestCase):
    """Test _ConnectionPool.get return values."""

    def test_get_returns_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn = pool.get(str(PROD_DB))
        self.assertIsNotNone(conn)
        self.assertIsInstance(conn, sqlite3.Connection)
        pool.close_all()

    def test_get_same_key_returns_same_connection(self):
        pool = _ConnectionPool(max_size=5)
        conn1 = pool.get(str(PROD_DB))
        conn2 = pool.get(str(PROD_DB))
        self.assertIs(conn1, conn2)
        pool.close_all()

    def test_get_different_keys_returns_different_connections(self):
        pool = _ConnectionPool(max_size=5)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db2 = f.name
        try:
            conn1 = pool.get(db1)
            conn2 = pool.get(db2)
            self.assertIsNot(conn1, conn2)
        finally:
            pool.close_all()
            for db in [db1, db2]:
                try:
                    os.unlink(db)
                except Exception:
                    pass


class TestCountRowsEdgeCases(unittest.TestCase):
    """Test count_rows edge cases."""

    def test_count_rows_nonexistent_db(self):
        count = count_rows(Path("/nonexistent/path/db.sqlite"))
        self.assertEqual(count, -1)

    def test_count_rows_empty_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp_db))
            conn.close()
            count = count_rows(tmp_db)
            # Should return -1 or 0
            self.assertIn(count, [-1, 0])
        finally:
            tmp_db.unlink(missing_ok=True)


class TestSafeCallEdgeCases(unittest.TestCase):
    """Test safe_call edge cases."""

    def test_returns_fallback_on_exception(self):
        result = safe_call(lambda: 1 / 0, fallback="error")
        self.assertEqual(result, "error")

    def test_returns_none_on_exception_no_fallback(self):
        result = safe_call(lambda: 1 / 0)
        self.assertIsNone(result)

    def test_returns_result_on_success(self):
        result = safe_call(lambda x: x * 2, 5)
        self.assertEqual(result, 10)


class TestAtomicWriteEdgeCases(unittest.TestCase):
    """Test atomic_write edge cases."""

    def test_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            result = atomic_write(tmp_path, "test")
            self.assertIsNone(result)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_creates_file(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            atomic_write(tmp_path, "content")
            self.assertTrue(tmp_path.exists())
        finally:
            tmp_path.unlink(missing_ok=True)


class TestOpenDbReturn(unittest.TestCase):
    """Test open_db return value."""

    def test_returns_connection(self):
        # Use write=False to get a direct sqlite3.Connection for read-only tests
        result = open_db(PROD_DB, write=False, pooled=True)
        self.assertIsNotNone(result)
        # Should be a context manager
        with result as conn:
            self.assertIsNotNone(conn)
            self.assertIsInstance(conn, sqlite3.Connection)


class TestValidateConfig(unittest.TestCase):
    """Test validate_config function."""

    def test_returns_list(self):
        from memory_common import validate_config

        result = validate_config()
        self.assertIsInstance(result, list)

    def test_returns_warnings_list(self):
        from memory_common import validate_config

        result = validate_config()
        # Each item should be a string
        for item in result:
            self.assertIsInstance(item, str)


class TestParseFrontmatter(unittest.TestCase):
    """Test parse_frontmatter function."""

    def test_returns_tuple(self):
        from memory_common import parse_frontmatter

        content = "---\ntitle: test\n---\n\nBody content"
        result = parse_frontmatter(content)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_parses_metadata(self):
        from memory_common import parse_frontmatter

        content = "---\ntitle: test\ncategory: lessons\n---\n\nBody"
        metadata, body = parse_frontmatter(content)
        self.assertEqual(metadata.get("title"), "test")
        self.assertEqual(metadata.get("category"), "lessons")

    def test_strips_quotes(self):
        from memory_common import parse_frontmatter

        content = '---\ntitle: "quoted value"\n---\n\nBody'
        metadata, body = parse_frontmatter(content)
        self.assertEqual(metadata.get("title"), "quoted value")


class TestCoerce(unittest.TestCase):
    """Test _coerce function."""

    def test_strips_double_quotes(self):
        from memory_common import _coerce

        result = _coerce('"hello"')
        self.assertEqual(result, "hello")

    def test_strips_single_quotes(self):
        from memory_common import _coerce

        result = _coerce("'hello'")
        self.assertEqual(result, "hello")

    def test_no_quotes(self):
        from memory_common import _coerce

        result = _coerce("hello")
        self.assertEqual(result, "hello")


class TestGetMemoryPaths(unittest.TestCase):
    """Test get_memory_paths function."""

    def test_returns_tuple(self):
        from memory_common import get_memory_paths

        result = get_memory_paths()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_returns_paths(self):
        from memory_common import get_memory_paths

        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsInstance(project_root, Path)
        self.assertIsInstance(local_mem, Path)
        self.assertIsInstance(global_mem, Path)


if __name__ == "__main__":
    unittest.main()
