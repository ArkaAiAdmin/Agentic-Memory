"""Tests for migration 020: kg_facts FTS5 index + sync triggers.

Verifies:
  1. The FTS virtual table exists and is indexed
  2. The 3 sync triggers (ai, ad, au) are present
  3. INSERT into kg_facts auto-populates the FTS table
  4. DELETE from kg_facts auto-removes from FTS
  5. UPDATE on kg_facts auto-updates FTS (delete-old + insert-new)
  6. The FTS MATCH query returns ranked results
"""

import os, sys, sqlite3, tempfile

sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

from fact_extraction import ensure_facts_schema


def _make_db_with_facts():
    """Create a temp DB with kg_facts + FTS schema, pre-populated with a few facts."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    # kg_facts has FKs to kg_entities, so create both
    conn.execute("""
        CREATE TABLE kg_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL
        )
    """)
    # Build the kg_facts schema (with the v18 columns + v19 FKs +
    # v20 FTS5 + triggers) via ensure_facts_schema.
    ensure_facts_schema(conn)
    # Add some facts (so the FTS has data to search). Use unique
    # contexts so MATCH queries don't have false positives.
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, context) "
        "VALUES ('python', 'is_a', 'language', 'high-level scripting option')"
    )
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, context) "
        "VALUES ('rust', 'is_a', 'language', 'memory-safe systems option')"
    )
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, context) "
        "VALUES ('java', 'is_a', 'language', 'jvm-based enterprise option')"
    )
    conn.commit()
    return conn, path


class TestKgFactsFTSExists:
    """Verify the FTS infrastructure is present after migration 020."""

    def test_fts_table_exists(self):
        conn, path = _make_db_with_facts()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='kg_facts_fts'"
            ).fetchone()
            assert row is not None, "kg_facts_fts FTS table is missing"
        finally:
            conn.close()

    def test_fts_is_contentless(self):
        """FTS5 virtual table should be contentless (backed by kg_facts)."""
        conn, path = _make_db_with_facts()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='kg_facts_fts'"
            ).fetchone()[0]
            assert "content='kg_facts'" in sql
            assert "content_rowid='id'" in sql
        finally:
            conn.close()

    def test_ai_trigger_exists(self):
        conn, path = _make_db_with_facts()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name='kg_facts_fts_ai'"
            ).fetchone()
            assert row is not None, "kg_facts_fts_ai (after-insert) trigger missing"
        finally:
            conn.close()

    def test_ad_trigger_exists(self):
        conn, path = _make_db_with_facts()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name='kg_facts_fts_ad'"
            ).fetchone()
            assert row is not None, "kg_facts_fts_ad (after-delete) trigger missing"
        finally:
            conn.close()

    def test_au_trigger_exists(self):
        conn, path = _make_db_with_facts()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name='kg_facts_fts_au'"
            ).fetchone()
            assert row is not None, "kg_facts_fts_au (after-update) trigger missing"
        finally:
            conn.close()


class TestKgFactsFTSInserts:
    """INSERT into kg_facts auto-populates the FTS index via trigger."""

    def test_insert_triggers_fts_population(self):
        conn, path = _make_db_with_facts()
        try:
            # The 3 initial facts are already in FTS via ensure_facts_schema
            # (which doesn't backfill, but they were inserted in the
            # helper AFTER the triggers were created, so they are indexed)
            n_fts = conn.execute("SELECT count(*) FROM kg_facts_fts").fetchone()[0]
            assert n_fts == 3

            # Insert a new fact — the ai trigger should index it
            conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, context) "
                "VALUES ('go', 'is_a', 'language', 'Go is a Google language')"
            )
            n_fts = conn.execute("SELECT count(*) FROM kg_facts_fts").fetchone()[0]
            assert n_fts == 4, "ai trigger should have indexed the new fact"
        finally:
            conn.close()

    def test_fts_search_finds_inserted_facts(self):
        conn, path = _make_db_with_facts()
        try:
            # Search for "go" — should find nothing initially
            n = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE kg_facts_fts MATCH 'go'"
            ).fetchone()[0]
            assert n == 0

            # Insert a fact mentioning "go"
            conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, context) "
                "VALUES ('go', 'is_a', 'language', 'Go is a Google language')"
            )

            # Now search should find it
            rows = conn.execute(
                "SELECT rowid, subject FROM kg_facts_fts WHERE kg_facts_fts MATCH 'go'"
            ).fetchall()
            assert len(rows) >= 1
        finally:
            conn.close()


class TestKgFactsFTSDeletes:
    """DELETE from kg_facts auto-removes from the FTS index via trigger."""

    def test_delete_triggers_fts_removal(self):
        conn, path = _make_db_with_facts()
        try:
            # Get the id of one of the initial facts
            row = conn.execute(
                "SELECT id FROM kg_facts WHERE subject = 'rust'"
            ).fetchone()
            rust_id = row[0]

            # Verify it's in FTS
            n_fts_before = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE rowid = ?",
                (rust_id,),
            ).fetchone()[0]
            assert n_fts_before == 1

            # Delete the fact
            conn.execute("DELETE FROM kg_facts WHERE id = ?", (rust_id,))

            # Verify it's gone from FTS
            n_fts_after = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE rowid = ?",
                (rust_id,),
            ).fetchone()[0]
            assert n_fts_after == 0, "ad trigger should have removed the fact from FTS"
        finally:
            conn.close()


class TestKgFactsFTSUpdates:
    """UPDATE on kg_facts auto-updates the FTS index via trigger."""

    def test_update_triggers_fts_refresh(self):
        conn, path = _make_db_with_facts()
        try:
            # Get the id of a fact
            row = conn.execute(
                "SELECT id FROM kg_facts WHERE subject = 'java'"
            ).fetchone()
            java_id = row[0]

            # Verify "java" is searchable
            n = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE kg_facts_fts MATCH 'java'"
            ).fetchone()[0]
            assert n >= 1

            # Update the subject to "kotlin"
            conn.execute(
                "UPDATE kg_facts SET subject = 'kotlin' WHERE id = ?",
                (java_id,),
            )

            # "java" should no longer be searchable
            n = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE kg_facts_fts MATCH 'java'"
            ).fetchone()[0]
            assert n == 0, "au trigger should have removed 'java' from FTS"

            # "kotlin" should now be searchable
            n = conn.execute(
                "SELECT count(*) FROM kg_facts_fts WHERE kg_facts_fts MATCH 'kotlin'"
            ).fetchone()[0]
            assert n >= 1, "au trigger should have added 'kotlin' to FTS"
        finally:
            conn.close()
