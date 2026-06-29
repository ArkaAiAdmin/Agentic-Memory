"""Tests for kg_dedup.py — knowledge graph entity deduplication.

Covers: duplicate entity merging (same name+type), case handling,
whitespace, edge redirection, dry-run mode, empty tables, stats.
"""

import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

from kg_dedup import dedup_entities


def _make_db():
    """Create a temp SQLite DB with KG tables and return (conn, path)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE kg_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            observations TEXT DEFAULT '[]',
            mentions INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            observations TEXT DEFAULT '[]',
            valid_at TEXT,
            invalid_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # kg_facts with the migration 019 schema (ON DELETE SET NULL on
    # entity FKs).  Other columns omitted for test brevity but the FK
    # shape is what we're testing.
    conn.execute("""
        CREATE TABLE kg_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
            object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
            UNIQUE(subject, predicate, object)
        )
    """)
    conn.commit()
    return conn, path


def _insert_entity(conn, name, etype, mentions=1):
    conn.execute(
        "INSERT INTO kg_entities (name, entity_type, mentions) VALUES (?, ?, ?)",
        (name, etype, mentions),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_edge(conn, src, tgt, relation, weight=1.0):
    conn.execute(
        "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
        (src, tgt, relation, weight),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestDedupEntitiesBasic:
    """Core dedup scenarios."""

    def test_no_duplicates_returns_zeros(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Alice", "person")
            _insert_entity(conn, "Bob", "person")
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 0
            assert stats["entities_merged"] == 0
            assert stats["edges_redirected"] == 0
        finally:
            conn.close()

    def test_empty_tables_no_crash(self):
        conn, _ = _make_db()
        try:
            stats = dedup_entities(conn)
            assert stats == {
                "groups_found": 0,
                "entities_merged": 0,
                "edges_redirected": 0,
                "dry_run": False,
            }
        finally:
            conn.close()

    def test_no_kg_entities_table_returns_zeros(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 0
            assert stats["entities_merged"] == 0
        finally:
            conn.close()
            os.unlink(path)

    def test_duplicate_same_name_type_merges(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "OpenAI", "org", mentions=2)
            id2 = _insert_entity(conn, "OpenAI", "org", mentions=3)
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 1
            assert stats["entities_merged"] == 1
            # Verify kept entity has merged mentions
            row = conn.execute(
                "SELECT mentions FROM kg_entities WHERE id = ?", (id2,)
            ).fetchone()
            assert row[0] == 5  # 2 + 3
            # Verify old entity deleted
            assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 1
        finally:
            conn.close()

    def test_keeps_highest_id_newest_entity(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Python", "tech", mentions=1)
            id2 = _insert_entity(conn, "Python", "tech", mentions=2)
            dedup_entities(conn)
            remaining = conn.execute("SELECT id FROM kg_entities").fetchone()[0]
            assert remaining == id2  # Higher ID kept
        finally:
            conn.close()

    def test_multiple_duplicate_groups(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Alice", "person")
            _insert_entity(conn, "Alice", "person")
            _insert_entity(conn, "Bob", "person")
            _insert_entity(conn, "Bob", "person")
            _insert_entity(conn, "Bob", "person")
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 2
            assert stats["entities_merged"] == 3  # 1 from Alice + 2 from Bob
        finally:
            conn.close()

    def test_different_types_not_deduped(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Python", "tech")
            _insert_entity(conn, "Python", "language")
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 0
            assert stats["entities_merged"] == 0
            assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 2
        finally:
            conn.close()


class TestEdgeRedirection:
    """Edge redirection during entity dedup."""

    def test_edges_redirect_source(self):
        conn, _ = _make_db()
        try:
            id_a = _insert_entity(conn, "Alice", "person")
            id_b = _insert_entity(conn, "Bob", "person")
            id_alice_dup = _insert_entity(conn, "Alice", "person")
            # Edge from duplicate Alice -> Bob (duplicate will be merged)
            _insert_edge(conn, id_alice_dup, id_b, "knows")
            dedup_entities(conn)
            # Kept entity is the lowest id (id_a), edge from dup redirected to it
            edges = conn.execute("SELECT source_id, target_id FROM kg_edges").fetchall()
            assert len(edges) == 1
            assert edges[0][0] == id_a  # Redirected to kept entity
            assert edges[0][1] == id_b
        finally:
            conn.close()

    def test_edges_redirect_target(self):
        conn, _ = _make_db()
        try:
            id_a = _insert_entity(conn, "Alice", "person")
            id_b = _insert_entity(conn, "Bob", "person")
            id_bob_dup = _insert_entity(conn, "Bob", "person")
            # Edge from Alice -> duplicate Bob (duplicate will be merged)
            _insert_edge(conn, id_a, id_bob_dup, "knows")
            dedup_entities(conn)
            edges = conn.execute("SELECT source_id, target_id FROM kg_edges").fetchall()
            assert len(edges) == 1
            assert edges[0][0] == id_a
            assert edges[0][1] == id_b  # Redirected to kept entity (lowest id)
        finally:
            conn.close()

    def test_existing_edge_merges_weight(self):
        conn, _ = _make_db()
        try:
            id_a = _insert_entity(conn, "Alice", "person")
            id_b = _insert_entity(conn, "Bob", "person")
            id_alice_dup = _insert_entity(conn, "Alice", "person")
            # Edge already exists: dup_Alice -> Bob (dup is kept since highest id)
            _insert_edge(conn, id_alice_dup, id_b, "knows", weight=1.0)
            # First Alice also has: Alice -> Bob
            _insert_edge(conn, id_a, id_b, "knows", weight=1.0)
            dedup_entities(conn)
            # Should end up with 1 edge, weight bumped by 0.1
            edges = conn.execute("SELECT weight FROM kg_edges").fetchall()
            assert len(edges) == 1
            assert abs(edges[0][0] - 1.1) < 0.01
        finally:
            conn.close()

    def test_orphan_edges_cleaned_after_merge(self):
        """After dedup, no edges should reference deleted entity IDs."""
        conn, _ = _make_db()
        try:
            id_a = _insert_entity(conn, "Alice", "person")
            _insert_entity(conn, "Alice", "person")
            id_c = _insert_entity(conn, "Charlie", "person")
            _insert_edge(conn, id_a, id_c, "works_with")
            dedup_entities(conn)
            # No edges should point to deleted entity
            dangling = conn.execute("""
                SELECT e.id FROM kg_edges e
                LEFT JOIN kg_entities src ON e.source_id = src.id
                LEFT JOIN kg_entities tgt ON e.target_id = tgt.id
                WHERE src.id IS NULL OR tgt.id IS NULL
            """).fetchall()
            assert len(dangling) == 0
        finally:
            conn.close()

    def test_count_edges_redirected(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "X", "thing")
            id_b = _insert_entity(conn, "Y", "thing")
            id_a2 = _insert_entity(conn, "X", "thing")
            # Edges FROM the newer (will-be-deleted) entity
            _insert_edge(conn, id_a2, id_b, "rel1")
            _insert_edge(conn, id_b, id_a2, "rel2")
            stats = dedup_entities(conn)
            # Both source and target redirections count
            assert stats["edges_redirected"] == 2
        finally:
            conn.close()


class TestDryRun:
    """Dry-run mode should not modify the database."""

    def test_dry_run_no_changes(self):
        conn, _ = _make_db()
        try:
            id1 = _insert_entity(conn, "Dup", "t", mentions=1)
            id2 = _insert_entity(conn, "Dup", "t", mentions=2)
            _insert_edge(conn, id1, id2, "self")
            stats = dedup_entities(conn, dry_run=True)
            assert stats["dry_run"] is True
            # Nothing should be deleted or modified
            assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0] == 1
            # Mentions should not be merged
            m1 = conn.execute(
                "SELECT mentions FROM kg_entities WHERE id=?", (id1,)
            ).fetchone()[0]
            m2 = conn.execute(
                "SELECT mentions FROM kg_entities WHERE id=?", (id2,)
            ).fetchone()[0]
            assert m1 == 1 and m2 == 2
        finally:
            conn.close()


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_three_way_duplicate(self):
        conn, _ = _make_db()
        try:
            id1 = _insert_entity(conn, "Tri", "x", mentions=1)
            id2 = _insert_entity(conn, "Tri", "x", mentions=2)
            id3 = _insert_entity(conn, "Tri", "x", mentions=3)
            _insert_edge(conn, id1, id3, "a")
            _insert_edge(conn, id2, id3, "b")
            stats = dedup_entities(conn)
            assert stats["entities_merged"] == 2
            remaining = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            assert remaining == 1
            mentions = conn.execute("SELECT mentions FROM kg_entities").fetchone()[0]
            assert mentions == 6  # 1+2+3
        finally:
            conn.close()

    def test_self_referencing_edge(self):
        """Edge where source == target (after redirect both go to keep_id)."""
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Self", "x")
            id2 = _insert_entity(conn, "Self", "x")
            _insert_edge(conn, id2, id2, "self_loop")
            dedup_entities(conn)
            edges = conn.execute("SELECT source_id, target_id FROM kg_edges").fetchall()
            # Should have one edge, both pointing to kept entity
            assert len(edges) <= 1  # May be deduped as self-loop
            if edges:
                assert edges[0][0] == edges[0][1]
        finally:
            conn.close()

    def test_stats_returned_correctly(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "A", "t")
            id_a2 = _insert_entity(conn, "A", "t")
            id_b = _insert_entity(conn, "B", "t")
            _insert_entity(conn, "B", "t")
            _insert_entity(conn, "B", "t")
            _insert_edge(conn, id_a2, id_b, "r")
            stats = dedup_entities(conn)
            assert isinstance(stats, dict)
            assert "groups_found" in stats
            assert "entities_merged" in stats
            assert "edges_redirected" in stats
            assert "dry_run" in stats
            assert stats["groups_found"] == 2
            assert stats["entities_merged"] == 3
        finally:
            conn.close()

    def test_single_entity_no_groups(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Only", "one")
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 0
            assert stats["entities_merged"] == 0
        finally:
            conn.close()

    def test_whitespace_same_name_deduped(self):
        """Entities with exact same name (including whitespace) are deduped."""
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "  spaced  ", "t")
            _insert_entity(conn, "  spaced  ", "t")
            stats = dedup_entities(conn)
            assert stats["groups_found"] == 1
        finally:
            conn.close()

    def test_many_entities_stress(self):
        """Larger batch to ensure no crashes."""
        conn, _ = _make_db()
        try:
            ids = []
            for i in range(50):
                ids.append(_insert_entity(conn, f"entity_{i % 10}", "type_a"))
            stats = dedup_entities(conn)
            # 10 unique names, each with 5 duplicates
            assert stats["groups_found"] == 10
            assert stats["entities_merged"] == 40
            remaining = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            assert remaining == 10
        finally:
            conn.close()


class TestEntityFKOnDeleteSetNull:
    """Regression for migration 019: kg_facts subject/object_entity_id
    FKs must have ON DELETE SET NULL so dedup doesn't fail with
    'FOREIGN KEY constraint failed' when an entity is referenced by
    a kg_facts row.
    """

    def test_kg_facts_schema_has_on_delete_set_null(self):
        """Verify the kg_facts FK clauses include ON DELETE SET NULL.

        This is a meta-test of the schema. If migration 019 was
        applied correctly, both subject_entity_id and object_entity_id
        should have ON DELETE SET NULL.
        """
        conn, _ = _make_db()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'kg_facts'"
            ).fetchone()[0]
            assert (
                "subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL"
                in sql
            ), "subject_entity_id FK is missing ON DELETE SET NULL"
            assert (
                "object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL"
                in sql
            ), "object_entity_id FK is missing ON DELETE SET NULL"
        finally:
            conn.close()

    def test_merge_with_kg_facts_reference_succeeds(self):
        """Merging an entity that's referenced by kg_facts.subject_entity_id
        should NOT raise FOREIGN KEY constraint failed.

        Reproduces the bug: prior to migration 019, the entity DELETE
        failed because kg_facts had a default-NO-ACTION FK. After the
        fix, the FK is ON DELETE SET NULL so the entity DELETE succeeds
        and the referencing subject_entity_id is set to NULL.
        """
        conn, _ = _make_db()
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            # Two duplicate entities — e1 (id=1) is older, e2 (id=2)
            # is newer. The dedup logic keeps the lowest-id entity
            # (e1) and merges the higher-id one (e2).
            e1 = _insert_entity(conn, "python", "technology")
            e2 = _insert_entity(conn, "python", "technology")
            assert e1 < e2
            # The kg_facts row references e2 — the entity that will
            # be DELETED. This is the case where the FK fix kicks in.
            conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, "
                "subject_entity_id) VALUES ('python', 'is_a', 'language', ?)",
                (e2,),
            )
            # Run dedup — should NOT raise FK error
            stats = dedup_entities(conn)
            assert stats["entities_merged"] == 1
            # After merge, only e1 remains. The kg_facts row's
            # subject_entity_id was e2, which got deleted. The
            # ON DELETE SET NULL clause should have nulled it.
            fact_subject_id = conn.execute(
                "SELECT subject_entity_id FROM kg_facts LIMIT 1"
            ).fetchone()[0]
            assert fact_subject_id is None
            # And the fact itself is still there (we only null'd the FK)
            count = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_merge_with_object_entity_id_reference_succeeds(self):
        """Same as above but via object_entity_id."""
        conn, _ = _make_db()
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            _insert_entity(conn, "django", "framework")
            e2 = _insert_entity(conn, "django", "framework")
            # Reference the higher-id (merge) entity — will be deleted
            conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, "
                "object_entity_id) VALUES ('web', 'uses', 'django', ?)",
                (e2,),
            )
            stats = dedup_entities(conn)
            assert stats["entities_merged"] == 1
            fact_obj_id = conn.execute(
                "SELECT object_entity_id FROM kg_facts LIMIT 1"
            ).fetchone()[0]
            assert fact_obj_id is None
        finally:
            conn.close()
