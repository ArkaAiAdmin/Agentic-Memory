"""Verify that recent migrations (038-040) apply cleanly on a fresh DB.

Migration 038 adds fingerprint + inception_at to kg_entities.
Migration 039 backfills belief_assertions for existing kg_facts.
Migration 040 creates belief_review_queue.
"""
import sqlite3

from infra.migration_runner import run_migrations


def _fresh_conn():
    """Create an in-memory SQLite connection (no temp-file cleanup needed)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class TestMigrationApplied:
    """Migrations 038-040 should apply without errors on a fresh DB."""

    def test_fingerprint_columns_exist(self):
        """kg_entities gains fingerprint and inception_at columns (migration 038)."""
        conn = _fresh_conn()
        try:
            run_migrations(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_entities)").fetchall()}
            assert "fingerprint" in cols, f"Expected 'fingerprint' in kg_entities, got {cols}"
            assert "inception_at" in cols, f"Expected 'inception_at' in kg_entities, got {cols}"
        finally:
            conn.close()

    def test_belief_assertions_exists(self):
        """belief_assertions table is created (migration 026)."""
        conn = _fresh_conn()
        try:
            run_migrations(conn)
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            assert "belief_assertions" in tables, (
                f"Expected 'belief_assertions' in tables, got {tables}"
            )
        finally:
            conn.close()

    def test_belief_review_queue_exists(self):
        """belief_review_queue table is created (migration 040)."""
        conn = _fresh_conn()
        try:
            run_migrations(conn)
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            assert "belief_review_queue" in tables, (
                f"Expected 'belief_review_queue' in tables, got {tables}"
            )
        finally:
            conn.close()

    def test_kg_facts_has_temporal_columns(self):
        """kg_facts has valid_at, invalid_at, transaction_time (migration 018)."""
        conn = _fresh_conn()
        try:
            run_migrations(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_facts)").fetchall()}
            for expected in ("valid_at", "invalid_at", "transaction_time"):
                assert expected in cols, f"Expected '{expected}' in kg_facts, got {cols}"
        finally:
            conn.close()

    def test_migrations_idempotent(self):
        """Running migrations twice on the same connection should not fail."""
        conn = _fresh_conn()
        try:
            run_migrations(conn)
            # Second run should be a no-op
            run_migrations(conn)
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            assert "belief_assertions" in tables
            assert "belief_review_queue" in tables
        finally:
            conn.close()
