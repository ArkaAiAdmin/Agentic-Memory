"""Unit test suite for infra.migration_runner resilience, multi-pass resolution, and fail-closed gates."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from infra.migration_runner import (
    SCHEMA_VERSION,
    _get_checksums,
    _parse_sql_file,
    migrate_down,
    run_migrations,
    verify_checksums,
)


class TestMigrationRunnerResilience(unittest.TestCase):
    """Test migration_runner multi-pass resolution, fail-closed handling, and locks."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_migration.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_run_migrations_success_and_checksum_integrity(self):
        """Verify clean forward migration and checksum recording."""
        run_migrations(self.conn)
        row = self.conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], SCHEMA_VERSION)

        mismatches = verify_checksums(self.conn)
        self.assertEqual(mismatches, [])

    def test_migrate_down_roundtrip(self):
        """Verify full roundtrip migration: 000 -> SCHEMA_VERSION -> 0 -> SCHEMA_VERSION."""
        run_migrations(self.conn)
        migrate_down(self.conn, 0)
        row = self.conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)

        # Re-apply forward
        run_migrations(self.conn)
        row_reapply = self.conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        self.assertEqual(row_reapply[0], SCHEMA_VERSION)

    def test_fail_closed_on_unresolved_forward_ref(self):
        """Verify fail-closed RuntimeError when a migration contains invalid table reference."""
        mock_sql = Path(self.tmp_dir.name) / "999_broken_ref.sql"
        mock_sql.write_text("ALTER TABLE non_existent_table_xyz ADD COLUMN test_col TEXT;")

        from unittest.mock import patch
        with patch("infra.migration_runner._get_available_migrations") as mock_avail:
            mock_avail.return_value = [(999, mock_sql)]
            with patch("infra.migration_runner.SCHEMA_VERSION", 999):
                with patch("infra.migration_runner.SCHEMA_STABLE", False):
                    with self.assertRaises(RuntimeError) as ctx:
                        run_migrations(self.conn)
                    self.assertIn("unresolved forward-reference statement", str(ctx.exception).lower())

    def test_environment_override_allows_deferred(self):
        """Verify MEMORY_ALLOW_DEFERRED_MIGRATIONS=1 allows soft deferral log without crashing."""
        mock_sql = Path(self.tmp_dir.name) / "999_broken_ref.sql"
        mock_sql.write_text("ALTER TABLE non_existent_table_xyz ADD COLUMN test_col TEXT;")

        from unittest.mock import patch
        with patch.dict(os.environ, {"MEMORY_ALLOW_DEFERRED_MIGRATIONS": "1"}):
            with patch("infra.migration_runner._get_available_migrations") as mock_avail:
                mock_avail.return_value = [(999, mock_sql)]
                with patch("infra.migration_runner.SCHEMA_VERSION", 999):
                    with patch("infra.migration_runner.SCHEMA_STABLE", False):
                        run_migrations(self.conn)

    def test_parse_sql_file_trigger_blocks(self):
        """Verify _parse_sql_file handles semicolons inside BEGIN...END trigger blocks."""
        mock_sql = Path(self.tmp_dir.name) / "test_trigger.sql"
        mock_sql.write_text(
            "CREATE TABLE t1 (id INT);\n"
            "CREATE TRIGGER tr1 AFTER INSERT ON t1 BEGIN UPDATE t1 SET id=id+1; END;\n"
            "CREATE INDEX idx1 ON t1(id);"
        )
        stmts = _parse_sql_file(mock_sql)
        self.assertEqual(len(stmts), 3)
        self.assertTrue(stmts[1].startswith("CREATE TRIGGER"))
        self.assertTrue(stmts[1].endswith("END"))


if __name__ == "__main__":
    unittest.main()
