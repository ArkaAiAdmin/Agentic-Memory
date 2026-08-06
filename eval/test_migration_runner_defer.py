#!/usr/bin/env python3
"""Regression tests for migration_runner suppressed-error deferral (C13)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.migration_runner import run_migrations


class TestMigrationRunnerDefer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_suppressed_error_migration_is_deferred_not_recorded(self):
        """Verify that a migration hitting a suppressed error (e.g. table missing) is not marked applied in db_migrations."""
        # Create a dummy migrations directory with two migrations
        mig_dir = Path(self.temp_dir.name) / "migrations"
        mig_dir.mkdir(parents=True, exist_ok=True)

        # 000: base migration creating migration tracking tables
        (mig_dir / "000_base.sql").write_text(
            "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);\n"
            "CREATE TABLE IF NOT EXISTS db_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT);"
        )

        # 001: valid migration
        (mig_dir / "001_initial.sql").write_text("CREATE TABLE base_table (id INT PRIMARY KEY);")

        # 002: migration with a query targeting non-existent table (suppressed error)
        (mig_dir / "002_dependent.sql").write_text(
            "ALTER TABLE non_existent_table ADD COLUMN extra TEXT;\n"
            "CREATE TABLE dependent_table (id INT PRIMARY KEY);"
        )

        import sqlite3, os
        with mock.patch("infra.migration_runner.MIGRATIONS_DIR", mig_dir), mock.patch.dict(os.environ, {"MEMORY_ALLOW_DEFERRED_MIGRATIONS": "1"}):
            conn = sqlite3.connect(self.db_path)
            try:
                run_migrations(conn)

                # Check schema_version table: 001 applied (version=1), 002 deferred (not version=2)
                row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], 1)

                # Now create non_existent_table manually and re-run migrations
                conn.execute("CREATE TABLE non_existent_table (id INT PRIMARY KEY);")
                conn.commit()
                run_migrations(conn)

                row_after = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
                self.assertEqual(row_after[0], 2)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
