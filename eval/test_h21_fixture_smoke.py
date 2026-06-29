"""
Smoke test for the H21 conftest fixtures.

Verifies that:
  - bootstrap_temp_db() creates a fully-bootstrapped DB
  - temp_db_path pytest fixture works
  - Both columns (deleted_at, deleted_by) are present
  - The schema version is current (5)
"""

import tempfile
from pathlib import Path


from memory_common import open_db


class TestBootstrapTempDb:
    def test_bootstrap_temp_db_function(self):
        """bootstrap_temp_db() copies the live prod schema to a target path."""
        from _fixtures import bootstrap_temp_db

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            bootstrap_temp_db(db)
            assert db.exists()
            assert db.stat().st_size > 0
            # Verify both soft-delete columns are present
            with open_db(db) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            assert "deleted_at" in cols, "deleted_at column missing"
            assert "deleted_by" in cols, "deleted_by column missing"

    def test_temp_db_path_fixture(self, temp_db_path):
        """The temp_db_path pytest fixture yields a fully-bootstrapped DB path."""
        assert temp_db_path.exists()
        assert temp_db_path.stat().st_size > 0
        # Verify the schema is the same as prod
        with open_db(temp_db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            # schema_version is stored in a singleton table, not PRAGMA user_version
            version = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
        assert "deleted_at" in cols
        assert "deleted_by" in cols
        # Schema version 6 = all 6 migrations applied (current is 6)
        assert version >= 6, f"expected schema version >= 6, got {version}"

    def test_temp_db_path_is_fresh_per_test(self, temp_db_path):
        """Each test gets a fresh DB (no shared state)."""
        # Write a row, then close. We need to include source_file (NOT NULL).
        with open_db(temp_db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, category, source_file, "
                "created_at, updated_at, observed_at) "
                "VALUES ('lessons/fixture-leak-test', 'leak', 'lessons', "
                "'memory/lessons/fixture-leak-test.md', "
                "'2026-06-16 00:00:00', '2026-06-16 00:00:00', '2026-06-16 00:00:00')"
            )

    def test_temp_db_path_no_leak_from_previous_test(self, temp_db_path):
        """The row written in the previous test must NOT be here."""
        with open_db(temp_db_path) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id='lessons/fixture-leak-test'"
            ).fetchone()
        assert row is None, "temp DB leaked state from previous test"
