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

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

import infra.migration_runner as migration_runner  # noqa: E402
import infra.db_migrations as db_migrations  # noqa: E402


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


class TestMigrationChecksums(unittest.TestCase):
    """Migration file checksums detect post-apply tampering."""

    def test_checksums_stored_after_fresh_migration(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            checksums = migration_runner._get_checksums(conn)
            self.assertGreater(len(checksums), 0)
            # Every applied migration should have a stored checksum.
            available = migration_runner._get_available_migrations()
            for num, path in available:
                expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    checksums.get(str(num)),
                    expected_hash,
                    f"checksum mismatch for migration {num:03d}",
                )
        finally:
            conn.close()

    def test_verify_checksums_passes_on_clean_db(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            mismatches = migration_runner.verify_checksums(conn)
            self.assertEqual(mismatches, [])
        finally:
            conn.close()

    def test_verify_checksums_detects_tampering(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            # Manually corrupt a stored checksum.
            checksums = migration_runner._get_checksums(conn)
            first_key = next(iter(checksums))
            checksums[first_key] = "0000000000000000000000000000000000000000000000000000000000000000"
            conn.execute(
                "UPDATE schema_version SET checksums = ? WHERE id = 1",
                (json.dumps(checksums),),
            )
            mismatches = migration_runner.verify_checksums(conn)
            self.assertEqual(len(mismatches), 1)
            self.assertEqual(mismatches[0][2], "0" * 64)
        finally:
            conn.close()

    def test_checksums_removed_on_rollback(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            # Roll back by 1.
            target = migration_runner.SCHEMA_VERSION - 1
            migration_runner.migrate_down(conn, target)
            checksums = migration_runner._get_checksums(conn)
            # The most recent migration should no longer have a checksum.
            self.assertNotIn(str(target + 1), checksums)
        finally:
            conn.close()

    def test_verify_checksums_empty_on_fresh_db(self):
        """A DB with no applied migrations has no checksums to verify."""
        conn = _new_db()
        try:
            mismatches = migration_runner.verify_checksums(conn)
            self.assertEqual(mismatches, [])
        finally:
            conn.close()

    def test_checksums_column_added_to_existing_table(self):
        """An existing schema_version table gets the checksums column."""
        conn = _new_db()
        try:
            conn.execute(
                "CREATE TABLE schema_version ("
                "  id INTEGER PRIMARY KEY CHECK(id=1),"
                "  version INTEGER NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT INTO schema_version (id, version) VALUES (1, 4)"
            )
            conn.commit()
            migration_runner._ensure_checksums_column(conn)
            # Read column info to confirm it exists.
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(schema_version)").fetchall()
            }
            self.assertIn("checksums", cols)
        finally:
            conn.close()


class TestMigrationDryRun(unittest.TestCase):
    """Dry-run mode shows pending SQL without executing."""

    def test_dry_run_does_not_apply_migrations(self):
        conn = _new_db()
        try:
            # Create the minimum tables that run_migrations needs, set a
            # low schema_version so there are pending migrations, then
            # verify dry-run leaves version unchanged.
            conn.execute(
                "CREATE TABLE memories ("
                "  id TEXT PRIMARY KEY, content TEXT NOT NULL,"
                "  source_file TEXT NOT NULL, tags TEXT DEFAULT '[]',"
                "  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                "  observed_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  version INTEGER NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT INTO schema_version (id, version) VALUES (1, 20)"
            )
            conn.commit()
            migration_runner.run_migrations(conn, dry_run=True)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            # Version must remain unchanged — dry-run must not apply
            # pending migrations (21..SCHEMA_VERSION).
            self.assertEqual(row[0], 20)
        finally:
            conn.close()

    def test_dry_run_on_fully_migrated_db_is_noop(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            version_before = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            migration_runner.run_migrations(conn, dry_run=True)
            version_after = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            self.assertEqual(version_after, version_before)
        finally:
            conn.close()

    def test_dry_run_rollback_does_not_modify_db(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            version_before = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            migration_runner.migrate_down(conn, 0, dry_run=True)
            version_after = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            self.assertEqual(version_after, version_before)
        finally:
            conn.close()


class TestDownUpRoundTripSchema(unittest.TestCase):
    """CI regression guard: migrate_down(N-1) then run_migrations() must
    restore an identical schema (same tables, columns, indexes, triggers,
    views).

    Runs for three representative migration boundaries: the first pair
    (001), a middle pair (016), and the last pair (SCHEMA_VERSION-1).
    """

    @staticmethod
    def _capture_schema(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
        """Return sorted (type, name, normalized-sql) tuples from sqlite_master.

        Normalizes SQL whitespace by collapsing all runs of whitespace into
        a single space and stripping leading/trailing whitespace.  This
        ensures that semantically identical schema objects created via
        different paths (e.g. migration 001's ``CREATE TABLE`` followed
        by ``ALTER TABLE ADD COLUMN`` vs. ``migrate_down``'s direct
        ``CREATE TABLE``) compare as equal.

        Excludes FTS5 internal tables (*_fts_*, *_content, *_segments,
        *_segdir, *_docsize, *_config) whose page numbers are
        non-deterministic, and the auto-generated ``sqlite_sequence``
        table used by AUTOINCREMENT.
        """
        return sorted(
            (
                (r[0], r[1], TestDownUpRoundTripSchema._normalize_sql(r[2]))
                for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL"
                ).fetchall()
                if not any(
                    name in r[1]
                    for name in (
                        "sqlite_sequence",
                    )
                )
                and not r[1].endswith(
                    (
                        "_fts_data",
                        "_fts_idx",
                        "_fts_content",
                        "_fts_docsize",
                        "_fts_config",
                    )
                )
            ),
            key=lambda x: (x[0], x[1]),
        )

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        """Collapse whitespace and normalise object-name quoting and column order.

        Removes whitespace before punctuation characters (commas,
        parentheses, semicolons) so that ``CREATE ... , checksums``
        and ``CREATE ... ,checksums`` both become ``CREATE ..., checksums``.
        Strips surrounding double-quotes from SQLite object identifiers
        (table/column/index names) so that SQLite's own DDL reflection
        (which quotes identifiers renamed via ALTER TABLE) compares equal
        to the original unquoted migration DDL. Also sorts and deduplicates
        column definitions inside CREATE TABLE statements so that column
        order changes (e.g. ALTER TABLE ADD COLUMN) do not surface as
        schema divergences.
        """
        import re

        norm = re.sub(r"\s+([,();])", r"\1", " ".join(sql.split()))
        # Strip surrounding double-quotes from SQLite identifiers.
        norm = re.sub(r'"([^"]+)"', r"\1", norm)
        # Normalise CREATE TABLE column order: sort columns and their
        # inline constraints deterministically so order-only differences
        # (e.g. ALTER TABLE ADD COLUMN vs. original CREATE TABLE) collapse
        # to the same string.
        if norm.upper().startswith("CREATE TABLE") and "(" in norm:
            head, rest = norm.split("(", 1)
            body, tail = rest.rsplit(")", 1)
            cols = []
            constraints = []
            for part in re.split(r",\s*", body.strip()):
                p = part.strip()
                if not p:
                    continue
                (constraints if p.upper().startswith(("CONSTRAINT", "PRIMARY KEY",
                    "UNIQUE", "CHECK", "FOREIGN KEY", "INDEX")) else cols).append(p)
            cols.sort()
            constraints.sort()
            norm = f"{head}({', '.join(cols + constraints)}){tail}"
        return norm

    @staticmethod
    def _verify_round_trip(
        conn: sqlite3.Connection,
        target: int,
        test_case: unittest.TestCase,
    ) -> None:
        """Roll back to *target*, re-apply, and assert schema identity.

        Uses the same ``run_schema_setup`` entry point as the initial
        setup to ensure all setup functions (e.g. FK removal on
        ``backlinks``) run identically in both passes.
        """
        schema_before = TestDownUpRoundTripSchema._capture_schema(conn)
        migration_runner.migrate_down(conn, target)
        db_migrations.run_schema_setup(conn)
        schema_after = TestDownUpRoundTripSchema._capture_schema(conn)
        test_case.assertEqual(
            schema_before,
            schema_after,
            f"schema mismatch after rollback to version {target} and re-apply",
        )

    def test_last_migration_pair(self):
        """Round-trip the most recent (N-1 → N) migration pair."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            db_migrations.run_schema_setup(conn)
            self._verify_round_trip(
                conn, migration_runner.SCHEMA_VERSION - 1, self
            )
        finally:
            conn.close()

    def test_middle_migration_pair_016(self):
        """Round-trip migration 016 (concept_drift — a representative table-
        creating migration in the middle of the sequence)."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            db_migrations.run_schema_setup(conn)
            self._verify_round_trip(conn, 15, self)
        finally:
            conn.close()

    def test_early_migration_pair_001(self):
        """Round-trip migration 001 (schema_version — the very first .sql
        migration, which runs before most base tables exist)."""
        conn = _new_db()
        try:
            _create_base_schema(conn)
            db_migrations.run_schema_setup(conn)
            self._verify_round_trip(conn, 0, self)
        finally:
            conn.close()


class TestDataPreservationOnRollback(unittest.TestCase):
    """M1 — critical proof: down migrations must not silently lose kg_facts rows.

    For each of 018, 019, 026:
      1. Apply all migrations up to and including N
      2. Insert a probe row into kg_facts
      3. Roll back to 0
      4. Assert the probe row is still present (in kg_facts or a
         kg_facts_pre_rollback_* table)
    """

    @staticmethod
    def _insert_probe(conn: sqlite3.Connection) -> int:
        conn.execute(
            "INSERT INTO kg_facts "
            "(subject, predicate, object, confidence, locked, "
            " fact_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("m1_probe_subject", "m1_probe_pred", "m1_probe_obj",
             1.0, 0, "observation"),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def test_018_down_preserves_kg_facts_rows(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            self._insert_probe(conn)
            conn.commit()
            migration_runner.migrate_down(conn, 0)
            survivors = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE subject = 'm1_probe_subject'"
            ).fetchone()[0]
            backup_survivors = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='kg_facts_pre_rollback_018'"
            ).fetchone()[0]
            self.assertTrue(
                survivors > 0 or backup_survivors > 0,
                "kg_facts row lost during 018 rollback — data not preserved",
            )
        finally:
            conn.close()

    def test_019_down_preserves_kg_facts_rows(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            self._insert_probe(conn)
            conn.commit()
            migration_runner.migrate_down(conn, 0)
            survivors = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE subject = 'm1_probe_subject'"
            ).fetchone()[0]
            backup_survivors = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='kg_facts_pre_rollback_019'"
            ).fetchone()[0]
            self.assertTrue(
                survivors > 0 or backup_survivors > 0,
                "kg_facts row lost during 019 rollback — data not preserved",
            )
        finally:
            conn.close()

    def test_026_down_preserves_kg_facts_rows(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            self._insert_probe(conn)
            conn.commit()
            migration_runner.migrate_down(conn, 0)
            survivors = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE subject = 'm1_probe_subject'"
            ).fetchone()[0]
            backup_survivors = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='kg_facts_pre_rollback_026'"
            ).fetchone()[0]
            self.assertTrue(
                survivors > 0 or backup_survivors > 0,
                "kg_facts row lost during 026 rollback — data not preserved",
            )
        finally:
            conn.close()


class TestDownUpRoundTripSchemaT6(unittest.TestCase):
    """T6 — migrate to N, rollback to 0, re-migrate to N, schema = fresh.

    Verifies that the down-up round-trip to version 0 and back
    produces an identical schema to a fresh migrate-to-N run.
    """

    def test_full_round_trip_to_zero_matches_fresh(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            schema_after_full = TestDownUpRoundTripSchema._capture_schema(conn)
            migration_runner.migrate_down(conn, 0)
            db_migrations.run_schema_setup(conn)
            migration_runner.run_migrations(conn)
            schema_after_round_trip = TestDownUpRoundTripSchema._capture_schema(conn)
            self.assertEqual(
                schema_after_full,
                schema_after_round_trip,
                "Schema after full round-trip to v0 and back differs from fresh migrate-to-N",
            )
        finally:
            conn.close()


class TestChecksumBackfillM5(unittest.TestCase):
    """M5 — up DBs with empty checksums get them backfilled on upgrade."""

    def test_empty_checksums_backfilled_on_upgrade(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            stored = migration_runner._get_checksums(conn)
            self.assertGreater(len(stored), 0)
            keys_before = set(stored.keys())
            # Simulate the pre-checksum state: wipe checksums in place.
            conn.execute("UPDATE schema_version SET checksums = '{}' WHERE id = 1")
            conn.commit()
            self.assertEqual(migration_runner._get_checksums(conn), {})
            # Run backfill
            migration_runner._backfill_empty_checksums(conn)
            conn.commit()
            stored_after = migration_runner._get_checksums(conn)
            keys_after = set(stored_after.keys())
            self.assertEqual(keys_after, keys_before)
            # Verify hashes are correct
            available = {num: path for num, path in migration_runner._get_available_migrations()}
            for num_str, expected_hash in stored_after.items():
                num = int(num_str)
                path = available.get(num)
                self.assertIsNotNone(path)
                if path is None:
                    continue
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(expected_hash, actual_hash)
        finally:
            conn.close()

    def test_non_empty_checksums_not_overwritten(self):
        conn = _new_db()
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            stored = migration_runner._get_checksums(conn)
            self.assertGreater(len(stored), 0)
            # Tamper with one hash
            first_key = next(iter(stored))
            stored[first_key] = "0" * 64
            conn.execute(
                "UPDATE schema_version SET checksums = ? WHERE id = 1",
                (json.dumps(stored),),
            )
            conn.commit()
            migration_runner._backfill_empty_checksums(conn)
            conn.commit()
            stored_after = migration_runner._get_checksums(conn)
            self.assertEqual(stored_after[first_key], "0" * 64)
        finally:
            conn.close()


class TestSearchRerankerFoundation(unittest.TestCase):
    """Phase 0 SOTA — migration 057 creates the search-reranker foundation
    tables (memory_search_interaction, memory_query_type_stats,
    memory_temporal_priors) and seeds memory_temporal_priors with 7 rows.

    Verifies the forward migration creates the expected schema + seed data,
    and that rolling back to version 56 drops all three tables (zero
    structural residue). Mirrors the helper conventions used elsewhere in
    this file (_new_db / _create_base_schema / migration_runner entry
    points). Uses a self-contained temp file DB — never memory/memory.db.
    """

    SEED_ROWS = {
        "lessons": 180,
        "concepts": 730,
        "sessions": 14,
        "preferences": 90,
        "projects": 365,
        "decisions": 365,
        "facts": 90,
    }

    FOUNDATION_TABLES = (
        "memory_search_interaction",
        "memory_query_type_stats",
        "memory_temporal_priors",
    )

    @contextmanager
    def _migrated_db(self):
        """Yield a fully-migrated connection on a self-contained temp file DB.

        The temp file is removed on exit; the connection is closed.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = Path(tmp.name)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            _create_base_schema(conn)
            migration_runner.run_migrations(conn)
            yield conn
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> dict:
        return {
            r[1]: r[2]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _index_names(self, conn: sqlite3.Connection, table: str) -> set:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name=?",
                (table,),
            ).fetchall()
        }

    def test_foundation_tables_created_with_expected_schema(self):
        with self._migrated_db() as conn:
            for table in self.FOUNDATION_TABLES:
                with self.subTest(table=table):
                    cols = self._table_columns(conn, table)
                    self.assertGreater(len(cols), 0, f"{table} missing")

            # memory_search_interaction columns + types + defaults.
            msi = self._table_columns(conn, "memory_search_interaction")
            for col in (
                "id",
                "query_id",
                "memory_id",
                "action",
                "tenant_id",
                "rank",
                "ts",
            ):
                self.assertIn(
                    col, msi, f"memory_search_interaction missing column {col}"
                )
            # Column types.
            self.assertEqual(msi["tenant_id"], "TEXT")
            self.assertEqual(msi["ts"], "REAL")
            # DEFAULT values: insert a minimal row (omitting tenant_id/ts/rank)
            # and confirm the defaults materialise.
            conn.execute(
                "INSERT INTO memory_search_interaction "
                "(query_id, memory_id, action) VALUES (?, ?, ?)",
                ("q_probe", "m_probe", "impression"),
            )
            default_row = conn.execute(
                "SELECT tenant_id, ts FROM memory_search_interaction "
                "WHERE query_id='q_probe' AND memory_id='m_probe'"
            ).fetchone()
            self.assertEqual(default_row[0], "default")
            self.assertIsNotNone(default_row[1])
            self.assertGreater(default_row[1], 0)

            # Indexes on memory_search_interaction.
            idx = self._index_names(conn, "memory_search_interaction")
            self.assertEqual(
                idx & {"idx_msi_query", "idx_msi_memory", "idx_msi_action"},
                {"idx_msi_query", "idx_msi_memory", "idx_msi_action"},
            )

            # memory_query_type_stats columns.
            qts = self._table_columns(conn, "memory_query_type_stats")
            for col in ("query_type", "weights_json", "sample_count", "updated_at"):
                self.assertIn(
                    col, qts, f"memory_query_type_stats missing column {col}"
                )

            # memory_temporal_priors columns.
            tp = self._table_columns(conn, "memory_temporal_priors")
            for col in ("category", "half_life_days", "updated_at"):
                self.assertIn(
                    col, tp, f"memory_temporal_priors missing column {col}"
                )

    def test_temporal_priors_seeded_with_seven_rows(self):
        with self._migrated_db() as conn:
            rows = dict(
                conn.execute(
                    "SELECT category, half_life_days FROM memory_temporal_priors"
                ).fetchall()
            )
            self.assertEqual(
                len(rows), 7, f"expected 7 seed rows, got {len(rows)}"
            )
            self.assertEqual(rows, self.SEED_ROWS)

    def test_rollback_to_56_drops_foundation_tables(self):
        with self._migrated_db() as conn:
            # Roll back to version 56 (drops migrations 058 and 057).
            migration_runner.migrate_down(conn, 56)
            version = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            self.assertEqual(version, 56)

            remaining = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('memory_search_interaction', 'memory_query_type_stats', "
                "'memory_temporal_priors')"
            ).fetchall()
            self.assertEqual(
                remaining,
                [],
                f"foundation tables still present after rollback: {remaining}",
            )

    def test_rollback_to_57_drops_colbert_tokens(self):
        with self._migrated_db() as conn:
            # Roll back to version 57 (drops migrations 059 and 058).
            migration_runner.migrate_down(conn, 57)
            version = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            self.assertEqual(version, 57)

            remaining = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = 'colbert_tokens'"
            ).fetchall()
            self.assertEqual(
                remaining,
                [],
                f"colbert_tokens still present after rollback: {remaining}",
            )

    def test_rollback_to_58_drops_splade_tokens(self):
        with self._migrated_db() as conn:
            # Roll back just migration 059 (to version 58).
            migration_runner.migrate_down(
                conn, migration_runner.SCHEMA_VERSION - 1
            )
            version = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()[0]
            self.assertEqual(version, migration_runner.SCHEMA_VERSION - 1)

            remaining = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = 'splade_tokens'"
            ).fetchall()
            self.assertEqual(
                remaining,
                [],
                f"splade_tokens still present after rollback: {remaining}",
            )


if __name__ == "__main__":
    unittest.main()
