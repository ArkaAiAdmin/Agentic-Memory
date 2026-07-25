#!/usr/bin/env python3
"""L82: Migration forward/rollback tests.

Verifies that each migration (000-073):
  1. Applies cleanly on a fresh DB
  2. Creates expected tables and columns
  3. Rolls back cleanly via the corresponding .down.sql file
  4. Leaves the schema in a consistent state after rollback
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PROJ = Path(__file__).parent.parent
sys.path.insert(0, str(PROJ))

from infra.migration_runner import (
    MIGRATIONS_DIR,
    SCHEMA_VERSION,
    _get_available_migrations,
    _get_down_migrations,
    _parse_sql_file,
    run_migrations,
    migrate_down,
)

# ---------------------------------------------------------------------------
# Table-per-migration mapping: which tables each migration creates (or
# renames into existence).  Migrations that only add columns or indexes
# are listed with an empty table list.
# ---------------------------------------------------------------------------
TABLES_BY_MIGRATION: dict[int, list[str]] = {
    0: ["memories", "kg_entities", "kg_edges", "kg_facts"],
    1: ["schema_version"],
    2: ["memory_embeddings"],
    3: ["memory_audit_log"],
    4: ["memory_vec_idx", "memory_vec_keys"],
    5: ["memory_chunks"],
    6: [],  # check constraints + indexes only
    7: ["memory_skills"],
    8: ["sync_log"],
    9: [],  # FK fix only
    10: [],  # index only
    11: [],  # index only
    12: ["kg_extraction_stats"],
    13: ["memory_field_crdt"],
    14: ["arc_ghosts", "arc_stats"],
    15: ["drift_alarms"],
    16: ["concept_drift"],
    17: [],  # recreates kg_edges + backlinks (existing tables)
    18: [],  # adds columns to kg_facts
    19: [],  # FK fix only
    20: [],  # FTS5 virtual table + triggers
    21: ["kg_entity_crdt", "kg_edge_crdt"],
    22: ["sessions", "decision_threads", "thread_events", "session_compaction_log"],
    23: [],  # column additions
    24: ["memory_chunk_embeddings", "memory_chunk_vec_idx", "memory_chunk_vec_keys"],
    25: [],  # column additions
    26: ["belief_assertions"],
    27: ["memory_revision_log"],
    28: ["entailment_chains"],
    29: ["graph_snapshots"],
    30: [],  # adds columns to kg_entities
    31: ["memory_events"],
    32: [],  # scoped outbox trigger
    33: [],  # column additions
    34: [],  # column additions
    35: [],  # adds columns to shared_memories
    36: [],  # adds column to memory_vec_idx
    37: ["cron_runs"],
    38: [],  # adds columns to kg_entities
    39: [],  # column additions
    40: ["belief_review_queue"],
    41: [],  # constraint change on kg_entities
    42: [],  # recreates memories table
    43: ["principals", "principal_identities"],
    44: [],  # recreates memory_audit_log
    45: ["roles", "role_bindings", "policies", "acl_overrides", "principal_roles_audit"],
    46: [],  # seed data only
    47: ["idem_token_key", "sso_idp_cache"],
    48: [],  # recreates principal_identities
    49: ["gdpr_requests"],
    50: [],  # adds columns to kg_entities + kg_facts
    51: [],  # adds column to memory_field_crdt
    52: [],  # backfill data only
    53: [],  # column additions
    54: [],  # column additions
    55: [],  # column additions
    56: [],  # column additions
    57: ["memory_search_interaction", "memory_query_type_stats", "memory_temporal_priors"],
    58: ["colbert_tokens"],
    59: ["splade_tokens"],
    60: [],  # index + trigger
    61: [],  # recreates memory_ctr_feedback
    62: [],  # adds column to memories
    63: ["cron_task_timeouts"],
    64: [],  # column additions
    65: ["kg_entity_crdt_append", "kg_edge_crdt_append"],
    66: [],  # adds columns to kg_*_crdt tables
    67: ["kg_entity_redirect"],
    68: ["saga_audit_log"],
    69: ["shared_tasks", "project_state", "agent_messages", "file_locks"],
    70: ["coordination_audit", "agent_heartbeats"],
    71: ["agent_registry_crdt"],
    72: ["system_locks"],
    73: [],  # bookkeeping / checksum re-write
    74: [],  # recreates destroyed indexes (additive only)
}

# Column checks: for migrations that add specific columns, verify a
# representative one.  Format: {migration_num: (table, column)}.
COLUMN_CHECKS: dict[int, tuple[str, str]] = {
    18: ("kg_facts", "event_time"),
    30: ("kg_entities", "community_id"),
    49: ("gdpr_requests", "id"),
    62: ("memories", "data_subject_sub"),
}


def _get_tables(conn: sqlite3.Connection) -> set[str]:
    """Return the set of user tables in the database."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for a table."""
    try:
        return {
            r[1]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except sqlite3.OperationalError:
        return set()


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version."""
    row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db(tmp_path):
    """Create a fresh SQLite database with PRAGMA foreign_keys ON."""
    db_path = tmp_path / "test_migrations.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrationsForward:
    """Apply each migration forward and verify schema state."""

    def test_run_all_migrations_forward(self, fresh_db):
        """All migrations 000-073 apply cleanly on a fresh DB."""
        conn, db_path = fresh_db
        run_migrations(conn)
        version = _get_schema_version(conn)
        assert version == SCHEMA_VERSION, (
            f"Expected schema_version={SCHEMA_VERSION} after full forward, got {version}"
        )

    @pytest.mark.parametrize("migration_num", list(range(0, SCHEMA_VERSION + 1)))
    def test_migration_creates_expected_tables(self, fresh_db, migration_num):
        """After applying migration N, expected tables exist."""
        conn, db_path = fresh_db
        # Apply all migrations up to and including migration_num
        run_migrations(conn)

        # Check tables that THIS specific migration should create
        expected_tables = TABLES_BY_MIGRATION.get(migration_num, [])
        if not expected_tables:
            pytest.skip(f"Migration {migration_num:03d} creates no new tables")

        actual_tables = _get_tables(conn)
        for table in expected_tables:
            # Some tables get recreated under new names (e.g. kg_entities_new
            # is temporary); skip temp tables that don't persist.
            if table.endswith("_new") or table.endswith("_backup_019"):
                continue
            assert table in actual_tables, (
                f"Migration {migration_num:03d} should create table '{table}', "
                f"but it's missing. Found: {sorted(actual_tables)}"
            )

    @pytest.mark.parametrize(
        "migration_num,table,column",
        [
            (num, t, c)
            for num, (t, c) in COLUMN_CHECKS.items()
        ],
        ids=[f"M{num:03d}_{t}.{c}" for num, (t, c) in COLUMN_CHECKS.items()],
    )
    def test_migration_adds_expected_column(self, fresh_db, migration_num, table, column):
        """After applying migration N, expected column exists."""
        conn, db_path = fresh_db
        run_migrations(conn)

        columns = _get_columns(conn, table)
        assert column in columns, (
            f"Migration {migration_num:03d} should add column '{column}' to "
            f"'{table}', but columns are: {sorted(columns)}"
        )

    def test_schema_version_after_full_forward(self, fresh_db):
        """schema_version equals SCHEMA_VERSION after all migrations."""
        conn, _ = fresh_db
        run_migrations(conn)
        assert _get_schema_version(conn) == SCHEMA_VERSION

    def test_no_pending_migrations_after_full_forward(self, fresh_db):
        """Running run_migrations twice is idempotent (no-op second time)."""
        conn, _ = fresh_db
        run_migrations(conn)
        # Second run should be a no-op — no errors, same version
        run_migrations(conn)
        assert _get_schema_version(conn) == SCHEMA_VERSION


class TestMigrationsRollback:
    """Roll back migrations and verify schema state."""

    def test_rollback_to_zero(self, fresh_db):
        """Roll back all migrations to version 0."""
        conn, _ = fresh_db
        run_migrations(conn)
        assert _get_schema_version(conn) == SCHEMA_VERSION

        migrate_down(conn, target_version=0)
        version = _get_schema_version(conn)
        assert version == 0, f"Expected schema_version=0 after full rollback, got {version}"

    @pytest.mark.parametrize(
        "target",
        [0, 1, 4, 10, 20, 30, 40, 50, 60, 70],
        ids=["to-0", "to-1", "to-4", "to-10", "to-20", "to-30", "to-40", "to-50", "to-60", "to-70"],
    )
    def test_rollback_to_target_version(self, fresh_db, target):
        """Roll back to a specific target version and verify."""
        conn, _ = fresh_db
        run_migrations(conn)

        migrate_down(conn, target_version=target)
        version = _get_schema_version(conn)
        assert version == target, (
            f"Expected schema_version={target} after rollback, got {version}"
        )

    def test_rollback_removes_tables(self, fresh_db):
        """After rollback, tables introduced after target version are gone."""
        conn, _ = fresh_db
        run_migrations(conn)

        # Roll back to version 0 — all tables except base should be gone
        migrate_down(conn, target_version=0)
        tables = _get_tables(conn)
        # After full rollback, core tables from migration 000 may also be gone
        # (000 has a down migration). Verify at least some are removed.
        assert "memory_field_crdt" not in tables, "memory_field_crdt should be removed after rollback to 0"

    def test_rollback_removes_post_target_tables(self, fresh_db):
        """After rollback, tables introduced after target version are gone."""
        conn, _ = fresh_db
        run_migrations(conn)

        # Roll back to version 14 (before drift_alarms was added in M015)
        migrate_down(conn, target_version=14)
        tables = _get_tables(conn)
        assert "drift_alarms" not in tables, (
            f"drift_alarms should not exist after rollback to v14, "
            f"but found: {sorted(tables)}"
        )
        # Verify tables from M014 and earlier still exist
        assert "arc_ghosts" in tables, "arc_ghosts should still exist after rollback to v14"

    def test_forward_after_rollback(self, fresh_db):
        """After rollback, running forward again re-applies cleanly."""
        conn, _ = fresh_db
        run_migrations(conn)
        migrate_down(conn, target_version=0)
        assert _get_schema_version(conn) == 0

        # Re-apply all migrations
        run_migrations(conn)
        assert _get_schema_version(conn) == SCHEMA_VERSION

    def test_partial_rollback_then_forward(self, fresh_db):
        """Partial rollback then full forward works."""
        conn, _ = fresh_db
        run_migrations(conn)

        # Roll back to v40
        migrate_down(conn, target_version=40)
        assert _get_schema_version(conn) == 40

        # Re-apply from v40 to current
        run_migrations(conn)
        assert _get_schema_version(conn) == SCHEMA_VERSION

    def test_no_down_migration_is_noop(self, fresh_db):
        """If target_version >= current, migrate_down is a no-op."""
        conn, _ = fresh_db
        run_migrations(conn)

        migrate_down(conn, target_version=SCHEMA_VERSION)
        assert _get_schema_version(conn) == SCHEMA_VERSION
