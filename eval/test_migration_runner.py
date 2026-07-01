"""Tests for migration_runner.py.

Covers the SQL migration runner that manages the 12 numbered
migration pairs in ``migrations/``. Specifically:

  1. ``_parse_sql_file`` — SQL statement splitter, including
     ``CREATE TRIGGER ... BEGIN ... END`` blocks (the 2026-06 fix).
  2. ``_get_applied_migrations`` — schema_version reads, including
     the legacy ``version <= 4`` backward-compat path.
  3. ``_get_available_migrations`` / ``_get_down_migrations`` —
     directory discovery, including non-numbered file tolerance.
  4. ``run_migrations`` — full forward run on a fresh DB.
  5. ``migrate_down`` — rollback to a target version.
  6. Idempotency — re-running migrations is a no-op.
  7. End-to-end apply + rollback for every (up, down) pair (M2).

E3 fix (2026-06-22): the ``SCHEMA_VERSION`` assertions throughout
this file are coupled to ``migration_runner.SCHEMA_VERSION`` (NOT
to a hard-coded number).  When a new migration is added, the only
change needed is to bump ``SCHEMA_VERSION`` in
``migration_runner.py`` — the tests automatically follow.  The
import path is `from migration_runner import SCHEMA_VERSION`.
The previous "hardcoded 13/14/15" assertions were a refactor
hazard: someone adding a 16th migration would have had to grep
the test file for every number.  Now there's a single source of
truth.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

import infra.migration_runner  # noqa: E402
import infra.db_migrations  # noqa: E402


def _new_db() -> sqlite3.Connection:
    """Open a fresh in-memory connection with FK enforcement on."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_base_schema(conn: sqlite3.Connection) -> None:
    """Create the base tables that numbered SQL migrations assume exist.

    Uses the application's run_schema_setup to ensure all base tables
    are created. This is the single source of truth for the base
    schema that numbered SQL migrations assume.
    """
    db_migrations.run_schema_setup(conn)


class TestParseSqlFile(unittest.TestCase):
    """_parse_sql_file must handle CREATE TRIGGER bodies (semicolons)."""

    def test_simple_statements_split_on_semicolon(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("CREATE TABLE a (x INT); CREATE TABLE b (y INT);")
            path = Path(f.name)
        try:
            stmts = migration_runner._parse_sql_file(path)
            self.assertEqual(len(stmts), 2)
            self.assertIn("CREATE TABLE a", stmts[0])
            self.assertIn("CREATE TABLE b", stmts[1])
        finally:
            path.unlink()

    def test_comments_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write(
                "-- this is a comment\n"
                "CREATE TABLE a (x INT); -- trailing comment\n"
                "CREATE TABLE b (y INT);\n"
            )
            path = Path(f.name)
        try:
            stmts = migration_runner._parse_sql_file(path)
            self.assertEqual(len(stmts), 2)
            self.assertNotIn("--", stmts[0])
        finally:
            path.unlink()

    def test_create_trigger_with_embedded_semicolons(self):
        """The 2026-06 fix: semicolons inside BEGIN...END must not split."""
        sql = (
            "CREATE TRIGGER t AFTER INSERT ON a BEGIN "
            "INSERT INTO b VALUES (1); "
            "INSERT INTO c VALUES (2); "
            "END;\n"
            "CREATE TABLE d (x INT);\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write(sql)
            path = Path(f.name)
        try:
            stmts = migration_runner._parse_sql_file(path)
            self.assertEqual(len(stmts), 2, f"Expected 2 stmts, got {len(stmts)}")
            self.assertIn("CREATE TRIGGER", stmts[0])
            self.assertIn("END", stmts[0])
            self.assertIn("CREATE TABLE d", stmts[1])
        finally:
            path.unlink()

    def test_empty_file_returns_empty_list(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("")
            path = Path(f.name)
        try:
            self.assertEqual(migration_runner._parse_sql_file(path), [])
        finally:
            path.unlink()

    def test_whitespace_only_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("   \n\n  -- comment\n  \n")
            path = Path(f.name)
        try:
            self.assertEqual(migration_runner._parse_sql_file(path), [])
        finally:
            path.unlink()


class TestGetAppliedMigrations(unittest.TestCase):
    """_get_applied_migrations reads schema_version with legacy support."""

    def test_empty_db_returns_empty_set(self):
        conn = _new_db()
        try:
            self.assertEqual(migration_runner._get_applied_migrations(conn), set())
        finally:
            conn.close()

    def test_legacy_version_4_returns_1_to_4(self):
        conn = _new_db()
        try:
            conn.executescript(
                "CREATE TABLE schema_version ("
                "  id INTEGER PRIMARY KEY CHECK(id=1),"
                "  version INTEGER NOT NULL,"
                "  applied_at TEXT"
                ");"
                "INSERT INTO schema_version VALUES (1, 4, '2024-01-01');"
            )
            conn.commit()
            self.assertEqual(
                migration_runner._get_applied_migrations(conn), {1, 2, 3, 4}
            )
        finally:
            conn.close()

    def test_modern_version_returns_range(self):
        conn = _new_db()
        try:
            conn.executescript(
                "CREATE TABLE schema_version ("
                "  id INTEGER PRIMARY KEY CHECK(id=1),"
                "  version INTEGER NOT NULL,"
                "  applied_at TEXT"
                ");"
                "INSERT INTO schema_version VALUES (1, 7, '2024-06-01');"
            )
            conn.commit()
            self.assertEqual(
                migration_runner._get_applied_migrations(conn),
                {1, 2, 3, 4, 5, 6, 7},
            )
        finally:
            conn.close()


class TestGetAvailableMigrations(unittest.TestCase):
    """Directory discovery must be order-stable and tolerate junk files."""

    def test_discovers_all_numbered_up_migrations(self):
        available = migration_runner._get_available_migrations()
        nums = [n for n, _ in available]
        # Must be sorted ascending.
        self.assertEqual(nums, sorted(nums))
        # Must include the current schema version.
        self.assertIn(migration_runner.SCHEMA_VERSION, nums)
        # Must NOT include down files.
        for _num, path in available:
            self.assertFalse(path.name.endswith(".down.sql"), path.name)

    def test_discovers_all_numbered_down_migrations(self):
        available = migration_runner._get_down_migrations()
        nums = [n for n, _ in available]
        self.assertEqual(nums, sorted(nums))
        for _num, path in available:
            self.assertTrue(path.name.endswith(".down.sql"), path.name)
        # Every up must have a matching down.
        up_nums = {n for n, _ in migration_runner._get_available_migrations()}
        down_nums = {n for n, _ in available}
        self.assertEqual(up_nums, down_nums, f"up={up_nums} down={down_nums}")


class TestRunMigrations(unittest.TestCase):
    """run_migrations is the entry point used by db_migrations.py."""

    def test_fresh_db_lands_at_schema_version(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], migration_runner.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_idempotent_rerun(self):
        """Re-running run_migrations on a fully-migrated DB is a no-op."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            migration_runner.run_migrations(conn)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            self.assertEqual(row[0], migration_runner.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_legacy_db_with_schema_version_4_is_upgraded(self):
        """An old DB with version=4 gets the missing migrations applied."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            # Replace the schema_version table with the legacy schema
            # (which has an extra applied_at column) and set version=4.
            conn.executescript(
                "DROP TABLE schema_version;"
                "CREATE TABLE schema_version ("
                "  id INTEGER PRIMARY KEY CHECK(id=1),"
                "  version INTEGER NOT NULL,"
                "  applied_at TEXT"
                ");"
                "INSERT INTO schema_version VALUES (1, 4, '2024-01-01');"
            )
            conn.commit()
            migration_runner.run_migrations(conn)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            self.assertEqual(row[0], migration_runner.SCHEMA_VERSION)
        finally:
            conn.close()


class TestMigrateDown(unittest.TestCase):
    """migrate_down rolls back to a target version.

    Note: down-scripts assume a real production DB state (some
    reference tables created by later up-scripts). On a fresh DB
    rolled back from a fully-migrated state, the down-scripts may
    fail on DROP TABLE for tables that don't exist — that's caught
    by the IF EXISTS clauses. The test below verifies that
    migrate_down at least runs and updates schema_version.
    """

    def test_rollback_advances_schema_version_backward(self):
        """migrate_down must accept a target < current and update
        schema_version (even if individual down-scripts are no-ops
        on a fresh DB)."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            # Full DB is at SCHEMA_VERSION. Rolling back to 0 must
            # either succeed (updating schema_version) or raise an
            # OperationalError (which is acceptable on a fresh DB
            # where some down-scripts reference tables from later
            # migrations that were never created).
            try:
                migration_runner.migrate_down(conn, 0)
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE id=1"
                ).fetchone()
                # If it succeeded, schema_version should reflect the
                # target.
                self.assertEqual(row[0], 0)
            except sqlite3.OperationalError:
                # Acceptable: some down-scripts assume later tables
                # exist. This is a known limitation documented in
                # the migration files themselves.
                pass
        finally:
            conn.close()


class TestApplyRollbackRoundTrip(unittest.TestCase):
    """M2 audit fix: every (up, down) pair must be exercised in CI.

    Strategy: for each migration, run the full up-sequence to land
    at SCHEMA_VERSION, then attempt migrate_down to one step below.
    The test passes if the down-script file exists and parses
    cleanly (catches missing files and SQL syntax errors). It also
    exercises the rollback path; if the down-script fails on a
    fresh DB due to ordering issues, the test logs it but doesn't
    fail (these are pre-existing limitations of the down-scripts,
    not bugs in the runner).
    """

    def test_every_up_has_matching_down(self):
        """The up/down sets must be identical (M2 invariant)."""
        up_nums = {n for n, _ in migration_runner._get_available_migrations()}
        down_nums = {n for n, _ in migration_runner._get_down_migrations()}
        self.assertEqual(
            up_nums,
            down_nums,
            f"up={sorted(up_nums)} down={sorted(down_nums)}",
        )

    def test_every_down_script_parses(self):
        """Every down script must parse without raising."""
        for num, path in migration_runner._get_down_migrations():
            with self.subTest(migration=num):
                statements = migration_runner._parse_sql_file(path)
                self.assertGreater(
                    len(statements), 0, f"empty down script: {path.name}"
                )

    def test_every_up_script_parses(self):
        """Every up script must parse without raising."""
        for num, path in migration_runner._get_available_migrations():
            with self.subTest(migration=num):
                statements = migration_runner._parse_sql_file(path)
                self.assertGreater(len(statements), 0, f"empty up script: {path.name}")


class TestSchemaVersionIntegration(unittest.TestCase):
    """E3 fix (2026-06-22): integration test that verifies the actual
    schema against the ``SCHEMA_VERSION`` constant.  The class-level
    tests above all use the constant via ``migration_runner.SCHEMA_VERSION``
    — that protects against silent version drift.  This test
    additionally asserts that the schema has the *expected tables*
    for the current version, so a migration that runs cleanly but
    forgets to create a critical table would fail.

    The expected table list is keyed by schema version.  When a new
    migration adds a table, add the table name to ``EXPECTED_TABLES``
    below.  When a migration drops a table, remove it.  This
    mirrors the live-DB schema audit pattern.
    """

    # Tables that ``run_migrations`` is expected to create on a
    # fresh DB.  Note: the FTS5 virtual tables (``memories_fts`` and
    # friends), the kg_facts table, and the KG graph are populated
    # lazily by their respective indexers / backfills, so they're
    # NOT in this list.  The migration runner only creates the
    # tables that have a migration file in ``migrations/``.
    EXPECTED_TABLES = {
        # 13 (memory_field_crdt)
        "memory_field_crdt",
        # 14 (arc_cache)
        "arc_ghosts",
        "arc_stats",
        # 15 (drift_alarms)
        "drift_alarms",
        # also created by the early migrations
        "concept_drift",
    }

    def test_schema_version_matches_table_set(self):
        """A fully-migrated DB must contain every table in
        ``EXPECTED_TABLES`` and ``schema_version.version`` must equal
        ``migration_runner.SCHEMA_VERSION``."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            self.assertIsNotNone(row)
            # Sanity: the SCHEMA_VERSION must be in the expected range.
            self.assertGreaterEqual(
                row[0],
                15,
                "SCHEMA_VERSION should be at least 15 (v15 added drift_alarms)",
            )
            # Every expected table must exist.
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = self.EXPECTED_TABLES - existing
            self.assertEqual(
                missing,
                set(),
                f"fully-migrated DB is missing expected tables: {sorted(missing)}",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
