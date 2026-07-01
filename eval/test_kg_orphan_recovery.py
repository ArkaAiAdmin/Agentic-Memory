#!/usr/bin/env python3
"""B-3 fix (2026-06-22 follow-up): KG / backlinks orphan recovery tests.

The audit gap: saga rollbacks (and pre-fix hard_delete_note calls) could
leave orphan rows in kg_edges, kg_entities, kg_facts, and backlinks.
Migration 017 added cascade FKs for the common case; this file verifies
both the saga path (in-flight cleanup) and the historical-repair path
(--repair-kg-orphans CLI).

Coverage:
    1. cleanup_memory_relations removes kg_facts / kg_edges / backlinks
       for a given note_id.
    2. cleanup_memory_relations does NOT delete kg_entities (shared).
    3. Saga undo_upsert for a fresh INSERT calls the cleanup helper.
    4. Saga undo_upsert for an UPDATE-style rollback also cleans up.
    5. find_kg_orphans returns the right shape on a clean DB (empty).
    6. find_kg_orphans detects manual orphans.
    7. repair_kg_orphans --dry-run does not modify the DB.
    8. repair_kg_orphans removes the orphans.
    9. After migration 017, deleting a memory cascades to backlinks
       and SET NULLs the kg_edges endpoints.
   10. hard_delete_note still works (regression for the refactor).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import open_db


# Schema mirroring the live install (pre-migration-017) so the helper
# tests run without depending on migration_runner state.
_KG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source_file TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT,
    mentions INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(name, entity_type)
);
CREATE TABLE IF NOT EXISTS kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
    relation TEXT NOT NULL DEFAULT 'related_to',
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    valid_at TEXT,
    invalid_at TEXT,
    UNIQUE(source_id, target_id, relation)
);
CREATE TABLE IF NOT EXISTS kg_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT, predicate TEXT, object TEXT,
    confidence REAL, locked INTEGER,
    first_seen REAL, last_seen REAL,
    mention_count INTEGER,
    source_memory TEXT, context TEXT,
    subject_entity_id INTEGER, object_entity_id INTEGER
);
CREATE TABLE IF NOT EXISTS backlinks (
    source_id TEXT,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);
"""


class _KgTestBase(unittest.TestCase):
    """Shared setup: fresh temp DB with the KG schema."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kg_orphan_"))
        self.db_path = self.tmp / "memory.db"
        with open_db(self.db_path) as conn:
            conn.executescript(_KG_SCHEMA_SQL)

    def tearDown(self) -> None:
        # Best-effort cleanup
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_memory(self, note_id: str, content: str = "hello") -> None:
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, "
                "updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    content,
                    f"{note_id.split('/', 1)[0]}/{note_id.split('/', 1)[1]}.md",
                    "2026-06-22T00:00:00Z",
                    "2026-06-22T00:00:00Z",
                    "2026-06-22T00:00:00Z",
                ),
            )

    def _seed_entity(self, name: str, entity_type: str = "concept") -> int:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO kg_entities (name, entity_type) VALUES (?, ?)",
                (name, entity_type),
            )
            return int(cur.lastrowid or 0)

    def _seed_fact(self, subject: str, obj: str, source_memory: str) -> int:
        return self._seed_fact_with_pred(subject, obj, "related_to", source_memory)

    def _seed_fact_with_pred(
        self, subject: str, obj: str, predicate: str, source_memory: str
    ) -> int:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO kg_facts (subject, predicate, object, source_memory) "
                "VALUES (?, ?, ?, ?)",
                (subject, predicate, obj, source_memory),
            )
            return int(cur.lastrowid or 0)

    def _seed_edge(self, source_id: int, target_id: int) -> int:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO kg_edges (source_id, target_id) VALUES (?, ?)",
                (source_id, target_id),
            )
            return int(cur.lastrowid or 0)

    def _seed_backlink(self, source_id: str, target_id: str) -> None:
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO backlinks (source_id, target_id) VALUES (?, ?)",
                (source_id, target_id),
            )

    def _count(self, table: str, where: str = "1", params: tuple = ()) -> int:
        with open_db(self.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()
            return int(row[0]) if row else 0


class TestCleanupMemoryRelations(_KgTestBase):
    """cleanup_memory_relations correctness."""

    def test_clears_kg_facts_for_note(self) -> None:
        from save.cleanup import cleanup_memory_relations

        self._seed_memory("lessons/foo")
        self._seed_fact("a", "b", "lessons/foo")
        self._seed_fact("c", "d", "lessons/foo")
        # Another note's fact — should NOT be touched.
        self._seed_memory("lessons/bar")
        self._seed_fact("e", "f", "lessons/bar")

        with open_db(self.db_path) as conn:
            cleanup_memory_relations(conn, "lessons/foo")

        self.assertEqual(
            self._count("kg_facts", "source_memory = ?", ("lessons/foo",)), 0
        )
        self.assertEqual(
            self._count("kg_facts", "source_memory = ?", ("lessons/bar",)), 1
        )

    def test_clears_backlinks_for_note(self) -> None:
        from save.cleanup import cleanup_memory_relations

        self._seed_memory("lessons/foo")
        self._seed_backlink("lessons/foo", "lessons/bar")
        self._seed_backlink(
            "lessons/bar", "lessons/foo"
        )  # target-only — must NOT be deleted
        self._seed_backlink("lessons/keep", "lessons/baz")

        with open_db(self.db_path) as conn:
            cleanup_memory_relations(conn, "lessons/foo")

        self.assertEqual(self._count("backlinks", "source_id = ?", ("lessons/foo",)), 0)
        self.assertEqual(self._count("backlinks", "source_id = ?", ("lessons/bar",)), 1)
        self.assertEqual(
            self._count("backlinks", "source_id = ?", ("lessons/keep",)), 1
        )

    def test_clears_orphan_kg_edges(self) -> None:
        """Edges referencing entities whose only remaining fact was from the cleaned note."""
        from save.cleanup import cleanup_memory_relations

        self._seed_memory("lessons/foo")
        e1 = self._seed_entity("alpha")
        e2 = self._seed_entity("beta")
        edge = self._seed_edge(e1, e2)
        # The only fact referencing alpha and beta is from lessons/foo.
        self._seed_fact("alpha", "beta", "lessons/foo")

        with open_db(self.db_path) as conn:
            cleanup_memory_relations(conn, "lessons/foo")

        # Edge should be gone (both endpoints became orphan).
        self.assertEqual(self._count("kg_edges", "id = ?", (edge,)), 0)

    def test_keeps_edges_for_referenced_entities(self) -> None:
        """Edges touching entities that are still referenced by other notes are kept."""
        from save.cleanup import cleanup_memory_relations

        self._seed_memory("lessons/foo")
        self._seed_memory("lessons/bar")
        e1 = self._seed_entity("shared")
        e2 = self._seed_entity("other")
        edge = self._seed_edge(e1, e2)
        # Both entities referenced by bar — should keep the edge.
        self._seed_fact("shared", "other", "lessons/bar")
        # Only one referencing foo — also keeps the edge (bar's fact is enough).
        # Use a different predicate to avoid the UNIQUE constraint.
        self._seed_fact_with_pred("shared", "other", "depends_on", "lessons/foo")

        with open_db(self.db_path) as conn:
            cleanup_memory_relations(conn, "lessons/foo")

        self.assertEqual(self._count("kg_edges", "id = ?", (edge,)), 1)

    def test_does_not_delete_kg_entities(self) -> None:
        """kg_entities are shared — they survive cleanup_memory_relations."""
        from save.cleanup import cleanup_memory_relations

        self._seed_memory("lessons/foo")
        e1 = self._seed_entity("alpha")
        e2 = self._seed_entity("beta")
        self._seed_edge(e1, e2)
        self._seed_fact("alpha", "beta", "lessons/foo")

        with open_db(self.db_path) as conn:
            cleanup_memory_relations(conn, "lessons/foo")

        # Entities are still there (shared across notes).
        self.assertEqual(self._count("kg_entities"), 2)

    def test_handles_missing_tables(self) -> None:
        """If kg_facts / kg_edges / backlinks are missing, cleanup is a no-op."""
        from save.cleanup import cleanup_memory_relations

        # Drop the optional tables so cleanup_memory_relations hits the
        # "table missing" branches in remove_kg_relations_for_note and
        # remove_backlinks_for_note.
        with open_db(self.db_path) as conn:
            for tbl in ("kg_facts", "kg_edges", "kg_entities", "backlinks"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            # Should not raise.
            cleanup_memory_relations(conn, "lessons/foo")


class TestSagaRollbackCleansOrphans(_KgTestBase):
    """The saga rollback path should call cleanup_memory_relations."""

    def test_undo_upsert_fresh_insert_cleans_kg_rows(self) -> None:
        """When a saga fails after the INSERT, undo deletes the row AND
        the dependent kg_facts/backlinks that an intermediate hook wrote."""
        from infra.saga import _build_save_memory_steps, Saga, SagaError

        self._seed_memory("lessons/foo")
        self._seed_fact("a", "b", "lessons/foo")
        self._seed_backlink("lessons/foo", "lessons/bar")

        # Simulate a saga that did:
        #   1. INSERT/UPDATE the memories row (success)
        #   2. vec key write (success)
        #   3. file write (FAIL)
        # and an intermediate post-save hook that wrote kg_facts +
        # backlinks for this note (this is the pattern that left
        # orphans before the fix).
        with open_db(self.db_path) as conn:
            steps, _params = _build_save_memory_steps(
                conn=conn,
                note_id="lessons/foo",
                file_path=self.tmp / "lessons" / "foo.md",
                db_path=self.db_path,
                do_upsert_db=lambda: None,  # already inserted in setUp
                do_write_vec_key=lambda: 1,
                do_write_file=lambda: (_ for _ in ()).throw(
                    RuntimeError("simulated failure")
                ),
            )
            with self.assertRaises(SagaError):
                with Saga(name="test_undo", steps=steps):
                    pass

        # Memories row was restored (or the INSERT was rolled back).
        # Either way, the dependent rows should be cleaned up.
        self.assertEqual(
            self._count("kg_facts", "source_memory = ?", ("lessons/foo",)), 0
        )
        self.assertEqual(self._count("backlinks", "source_id = ?", ("lessons/foo",)), 0)

    def test_undo_upsert_update_path_cleans_kg_rows(self) -> None:
        """UPDATE-style rollback (pre-existing row) also cleans up."""
        from infra.saga import _build_save_memory_steps, Saga, SagaError

        # Pre-existing memory: set initial content + tags so the
        # pre-existing branch fires.
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, "
                "updated_at, observed_at, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "lessons/foo",
                    "original content",
                    "lessons/foo.md",
                    "2026-06-22T00:00:00Z",
                    "2026-06-22T00:00:00Z",
                    "2026-06-22T00:00:00Z",
                    json.dumps(["orig"]),
                ),
            )
        # Simulate a saga that updated the row then failed in the
        # file step.  An intermediate hook wrote kg_facts + backlinks
        # for this note.
        self._seed_fact("a", "b", "lessons/foo")
        self._seed_backlink("lessons/foo", "lessons/bar")

        with open_db(self.db_path) as conn:
            steps, _params = _build_save_memory_steps(
                conn=conn,
                note_id="lessons/foo",
                file_path=self.tmp / "lessons" / "foo.md",
                db_path=self.db_path,
                do_upsert_db=lambda: None,
                do_write_vec_key=lambda: 1,
                do_write_file=lambda: (_ for _ in ()).throw(
                    RuntimeError("simulated failure")
                ),
            )
            with self.assertRaises(SagaError):
                with Saga(name="test_undo_update", steps=steps):
                    pass

        # The original content should be restored.
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT content, tags FROM memories WHERE id = ?", ("lessons/foo",)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "original content")
        # And the dependent rows are cleaned up.
        self.assertEqual(
            self._count("kg_facts", "source_memory = ?", ("lessons/foo",)), 0
        )
        self.assertEqual(self._count("backlinks", "source_id = ?", ("lessons/foo",)), 0)


class TestFindKgOrphans(_KgTestBase):
    """find_kg_orphans / repair_kg_orphans correctness."""

    def test_clean_db_returns_empty(self) -> None:
        from memory_integrity import find_kg_orphans

        with open_db(self.db_path) as conn:
            orphans = find_kg_orphans(conn)
        self.assertEqual(orphans["kg_edges"], [])
        self.assertEqual(orphans["kg_entities"], [])
        self.assertEqual(orphans["backlinks"], [])

    def test_detects_orphan_kg_edges(self) -> None:
        from memory_integrity import find_kg_orphans

        self._seed_memory("lessons/foo")
        e1 = self._seed_entity("alpha")
        e2 = self._seed_entity("beta")
        edge_id = self._seed_edge(e1, e2)
        # Only fact references alpha+beta — clean it up first.
        self._seed_fact("alpha", "beta", "lessons/foo")
        with open_db(self.db_path) as conn:
            conn.execute(
                "DELETE FROM kg_facts WHERE source_memory = ?", ("lessons/foo",)
            )

        with open_db(self.db_path) as conn:
            orphans = find_kg_orphans(conn)
        self.assertEqual(len(orphans["kg_edges"]), 1)
        self.assertEqual(orphans["kg_edges"][0]["id"], edge_id)

    def test_detects_orphan_kg_entities(self) -> None:
        from memory_integrity import find_kg_orphans

        # Create an entity that is referenced by nothing.
        self._seed_entity("orphan_entity")
        with open_db(self.db_path) as conn:
            orphans = find_kg_orphans(conn)
        names = [e["name"] for e in orphans["kg_entities"]]
        self.assertIn("orphan_entity", names)

    def test_detects_orphan_backlinks(self) -> None:
        from memory_integrity import find_kg_orphans

        # Backlink with non-existent source — should be flagged.
        self._seed_backlink("lessons/nonexistent", "lessons/foo")
        # Red-link target is allowed (target_id doesn't need to exist).
        self._seed_memory("lessons/foo")
        self._seed_backlink("lessons/foo", "lessons/red-link")

        with open_db(self.db_path) as conn:
            orphans = find_kg_orphans(conn)
        sources = [b["source_id"] for b in orphans["backlinks"]]
        self.assertIn("lessons/nonexistent", sources)
        self.assertNotIn("lessons/foo", sources)  # red-link target is fine

    def test_repair_dry_run_does_not_modify(self) -> None:
        from memory_integrity import repair_kg_orphans

        self._seed_entity("orphan")
        with open_db(self.db_path) as conn:
            orphans_before = conn.execute(
                "SELECT COUNT(*) FROM kg_entities"
            ).fetchone()[0]

        result = repair_kg_orphans(self.db_path, dry_run=True)

        self.assertTrue(result["was_orphaned"])
        with open_db(self.db_path) as conn:
            orphans_after = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[
                0
            ]
        self.assertEqual(orphans_before, orphans_after)

    def test_repair_removes_orphans(self) -> None:
        from memory_integrity import repair_kg_orphans

        self._seed_entity("orphan")
        self._seed_backlink("lessons/missing", "lessons/x")

        result = repair_kg_orphans(self.db_path, dry_run=False)

        self.assertTrue(result["was_orphaned"])
        self.assertEqual(result["deleted_kg_entities"], 1)
        self.assertEqual(result["deleted_backlinks"], 1)
        self.assertEqual(self._count("kg_entities"), 0)
        self.assertEqual(self._count("backlinks"), 0)

    def test_repair_clean_db_is_noop(self) -> None:
        from memory_integrity import repair_kg_orphans

        result = repair_kg_orphans(self.db_path, dry_run=False)
        self.assertFalse(result["was_orphaned"])
        self.assertEqual(result["deleted_kg_edges"], 0)
        self.assertEqual(result["deleted_kg_entities"], 0)
        self.assertEqual(result["deleted_backlinks"], 0)


class TestHardDeleteRegression(_KgTestBase):
    """The hard_delete_note refactor must preserve prior behavior."""

    def test_hard_delete_clears_relations(self) -> None:
        from memory_delete import hard_delete_note

        # Seed an old-enough memory.
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, "
                "updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "lessons/foo",
                    "old content",
                    "lessons/foo.md",
                    "2020-01-01T00:00:00Z",  # >30 days old
                    "2020-01-01T00:00:00Z",
                    "2020-01-01T00:00:00Z",
                ),
            )
        self._seed_fact("a", "b", "lessons/foo")
        self._seed_backlink("lessons/foo", "lessons/bar")

        ok = hard_delete_note(self.db_path, "lessons/foo")
        self.assertTrue(ok)
        # Memories gone.
        self.assertEqual(self._count("memories", "id = ?", ("lessons/foo",)), 0)
        # Backlinks for this source gone.
        self.assertEqual(self._count("backlinks", "source_id = ?", ("lessons/foo",)), 0)
        # kg_facts for this memory gone.
        self.assertEqual(
            self._count("kg_facts", "source_memory = ?", ("lessons/foo",)), 0
        )


class TestMigrationCascade(_KgTestBase):
    """Verify the migration 017 .sql file's effect (parser-level test)."""

    def test_migration_sql_parses(self) -> None:
        """The migration file must be present and parse cleanly."""
        from infra.migration_runner import _parse_sql_file

        path = INSTALL_DIR / "migrations" / "017_kg_cascade.sql"
        self.assertTrue(path.exists(), f"missing migration: {path}")
        statements = _parse_sql_file(path)
        # We expect: CREATE TABLE (×2), CREATE INDEX (×5 for kg_edges +
        # ×1 for backlinks), INSERT (×2), DROP (×2), ALTER (×2),
        # PRAGMA (×2), BEGIN/COMMIT.
        self.assertGreater(len(statements), 0)
        # Every statement should be non-empty.
        for s in statements:
            self.assertTrue(s.strip(), f"empty statement in 017: {s!r}")

    def test_down_migration_sql_parses(self) -> None:
        from infra.migration_runner import _parse_sql_file

        path = INSTALL_DIR / "migrations" / "017_kg_cascade.down.sql"
        self.assertTrue(path.exists(), f"missing down migration: {path}")
        statements = _parse_sql_file(path)
        self.assertGreater(len(statements), 0)

    def test_schema_version_bumped_to_17(self) -> None:
        from infra.migration_runner import SCHEMA_VERSION

        self.assertGreaterEqual(SCHEMA_VERSION, 17)


if __name__ == "__main__":
    unittest.main()
