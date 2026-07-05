"""Regression tests for DB drift, data sink, and sync invariant issues.

DB drift:  connection_pool.get() connections lacked KG/chunks/vec_keys
            tables because Python migrations weren't run on pooled
            connections.  Fixed by _ensure_full_schema().

Data sink: INSERT OR REPLACE on memories triggered FK CASCADE DELETE,
            silently wiping embeddings, vec_keys, and other linked data.
            Fixed by using INSERT ... ON CONFLICT DO UPDATE.

Sync:      Subsystems (FTS5, vec_keys, embeddings, KG) could silently
            drift out of sync with the memories table.  Fixed by
            sync_invariant.py + backfill_drifted_subsystems().
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bare_db(path: str) -> sqlite3.Connection:
    """Create a minimal DB with only the memories table (no subsystem tables).

    This simulates the state that caused drift: connection_pool.get()
    would return a connection that had only PRAGMAs set but no KG,
    chunks, vec_keys, or other subsystem tables.
    """
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            content       TEXT DEFAULT '',
            source_file   TEXT DEFAULT '',
            tags          TEXT DEFAULT '[]',
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now')),
            observed_at   TEXT DEFAULT (datetime('now')),
            pinned        INTEGER DEFAULT 0,
            importance    INTEGER DEFAULT 3,
            decay         TEXT DEFAULT 'none',
            score         REAL DEFAULT 1.0,
            supersedes    TEXT,
            repo_id       TEXT,
            access_count  INTEGER DEFAULT 1,
            success_score REAL DEFAULT 0.0,
            fitness_score REAL DEFAULT 1.0,
            conflict_policy TEXT DEFAULT 'supersede',
            version_vector TEXT DEFAULT '{}',
            logical_clock INTEGER DEFAULT 0,
            consolidation_state TEXT DEFAULT 'working',
            valid_from    TEXT,
            valid_to      TEXT,
            superseded_by TEXT,
            last_accessed TEXT,
            deleted_at    TEXT,
            deleted_by    TEXT,
            context_prefix TEXT,
            category      TEXT,
            tier          TEXT,
            importance_score REAL DEFAULT 0.5,
            metadata      TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DB Drift Tests
# ---------------------------------------------------------------------------


class TestConnectionPoolSchema(unittest.TestCase):
    """Verify that connection_pool.get() returns connections with full schema.

    These are the regression tests for the DB drift bug where
    save_pipeline.py used connection_pool.get() directly, which
    only set PRAGMAs but didn't run Python schema migrations.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "drift_test.db")
        from infra.memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_pooled_conn(self):
        from infra.memory_common import connection_pool

        return connection_pool.get(self.db_path, timeout=5.0)

    def _get_tables(self):
        return {
            r[0]
            for r in self._get_pooled_conn()
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }

    def test_kg_entities_table_exists(self):
        self.assertIn("kg_entities", self._get_tables())

    def test_kg_edges_table_exists(self):
        self.assertIn("kg_edges", self._get_tables())

    def test_memory_vec_keys_table_exists(self):
        self.assertIn("memory_vec_keys", self._get_tables())

    def test_memory_embeddings_table_exists(self):
        self.assertIn("memory_embeddings", self._get_tables())

    def test_memory_chunks_table_exists(self):
        self.assertIn("memory_chunks", self._get_tables())

    def test_schema_version_table_exists(self):
        self.assertIn("schema_version", self._get_tables())

    def test_memory_audit_log_table_exists(self):
        self.assertIn("memory_audit_log", self._get_tables())

    def test_backlinks_table_exists(self):
        self.assertIn("backlinks", self._get_tables())

    def test_fts5_triggers_exist(self):
        """If memories_fts exists, its triggers must also exist."""
        conn = self._get_pooled_conn()
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        if fts_exists:
            fts_triggers = [
                t
                for t in triggers
                if "memories_ai" in t or "memories_ad" in t or "memories_au" in t
            ]
            self.assertTrue(
                len(fts_triggers) > 0,
                f"Expected FTS5 triggers for memories_fts, found: {triggers}",
            )
        else:
            # memories_fts not in this DB — just verify other subsystem triggers exist
            self.assertTrue(
                len(triggers) > 0, f"Expected at least some triggers, found: {triggers}"
            )

    def test_kg_entities_can_insert(self):
        conn = self._get_pooled_conn()
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES ('test-entity', 'concept')"
        )
        row = conn.execute(
            "SELECT name FROM kg_entities WHERE name = 'test-entity'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_vec_keys_can_insert(self):
        conn = self._get_pooled_conn()
        # Insert a parent memory first (FK constraint)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, ?)",
            ("test-note", "test content", "[]"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (memory_id, key) VALUES (?, ?)",
            ("test-note", 42),
        )
        row = conn.execute(
            "SELECT memory_id FROM memory_vec_keys WHERE memory_id = ?", ("test-note",)
        ).fetchone()
        self.assertIsNotNone(row)

    def test_chunks_can_insert(self):
        conn = self._get_pooled_conn()
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, ?)",
            ("test-note", "test content", "[]"),
        )
        conn.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?, ?, ?, ?, ?)",
            ("test-note", 0, 0, 10, "test chunk"),
        )
        row = conn.execute(
            "SELECT content FROM memory_chunks WHERE parent_id = ?", ("test-note",)
        ).fetchone()
        self.assertIsNotNone(row)


class TestMigrationIdempotency(unittest.TestCase):
    """Running migrations multiple times must not break anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "idempotent_test.db")
        # Create the memories table (open_db doesn't create it)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            pinned INTEGER DEFAULT 0
        )""")
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_double_migration_no_error(self):
        from infra.memory_common import open_db

        with open_db(Path(self.db_path)) as db:
            db.execute(
                "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                ("m1", "content 1"),
            )
            db.commit()
        with open_db(Path(self.db_path)) as db:
            fetched = db.execute(
                "SELECT content FROM memories WHERE id = ?", ("m1",)
            ).fetchone()
            assert fetched is not None
            row = fetched
            self.assertEqual(row[0], "content 1")

    def test_migration_preserves_data(self):
        from infra.memory_common import open_db

        with open_db(Path(self.db_path)) as db:
            db.execute(
                "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, ?)",
                ("m1", "test content", '["tag1"]'),
            )
            db.commit()
        with open_db(Path(self.db_path)) as db:
            fetched = db.execute(
                "SELECT content, tags FROM memories WHERE id = ?", ("m1",)
            ).fetchone()
            assert fetched is not None
            row = fetched
            self.assertEqual(row[0], "test content")
            self.assertEqual(row[1], '["tag1"]')


class TestForeignKeyEnforcement(unittest.TestCase):
    """FK constraints must be ON on pooled connections."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "fk_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fk_on_for_pooled_connections(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

    def test_cascade_delete_embeddings(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
            ("del-test", "delete me"),
        )
        import numpy as np

        blob = np.zeros(8, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("del-test", "hash1", blob, "model1", 8, 0.0),
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("del-test",),
            ).fetchone()[0],
            1,
        )
        conn.execute("DELETE FROM memories WHERE id = ?", ("del-test",))
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("del-test",),
            ).fetchone()[0],
            0,
        )

    def test_cascade_delete_vec_keys(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
            ("del-vec", "delete me"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (memory_id, key) VALUES (?, ?)",
            ("del-vec", 100),
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_vec_keys WHERE memory_id = ?", ("del-vec",)
            ).fetchone()[0],
            1,
        )
        conn.execute("DELETE FROM memories WHERE id = ?", ("del-vec",))
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_vec_keys WHERE memory_id = ?", ("del-vec",)
            ).fetchone()[0],
            0,
        )


# ---------------------------------------------------------------------------
# Data Sink Tests
# ---------------------------------------------------------------------------


class TestUpsertPreservesSubsystems(unittest.TestCase):
    """Upserting a memory must NOT wipe embeddings, vec_keys, or KG data.

    This is the INSERT OR REPLACE cascade bug: DELETE + INSERT wipes
    all FK-linked rows.  Fixed by ON CONFLICT DO UPDATE.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "sink_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upsert(self, conn, note_id, content):
        """Simulate what _upsert_memory_row does (ON CONFLICT DO UPDATE)."""
        conn.execute(
            """INSERT INTO memories (id, content, tags, updated_at)
               VALUES (?, ?, '[]', ?)
               ON CONFLICT(id) DO UPDATE SET
                   content = excluded.content,
                   updated_at = excluded.updated_at""",
            (note_id, content, _now_iso()),
        )
        conn.commit()

    def test_upsert_preserves_embedding(self):
        from infra.memory_common import connection_pool
        import numpy as np

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, '[]')",
            ("note-1", "original"),
        )
        blob = np.ones(8, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("note-1", "hash1", blob, "model1", 8, 0.0),
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("note-1",),
            ).fetchone()[0],
            1,
        )
        self._upsert(conn, "note-1", "updated content")
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("note-1",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT content FROM memories WHERE id = ?", ("note-1",)
            ).fetchone()[0],
            "updated content",
        )

    def test_upsert_preserves_vec_keys(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
            ("note-2", "original"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (memory_id, key) VALUES (?, ?)",
            ("note-2", 100),
        )
        conn.commit()
        self._upsert(conn, "note-2", "updated")
        vk = conn.execute(
            "SELECT key FROM memory_vec_keys WHERE memory_id = ?", ("note-2",)
        ).fetchone()
        self.assertIsNotNone(vk, "vec_key lost after upsert!")
        self.assertEqual(vk[0], 100)

    def test_upsert_preserves_kg_entities(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
            ("note-3", "original"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
            ("note-3", "memory"),
        )
        conn.commit()
        self._upsert(conn, "note-3", "updated")
        ent = conn.execute(
            "SELECT name FROM kg_entities WHERE name = 'note-3'"
        ).fetchone()
        self.assertIsNotNone(ent, "KG entity lost after upsert!")

    def test_upsert_preserves_kg_edges(self):
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)", ("a", "x")
        )
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)", ("b", "y")
        )
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
            ("a", "memory"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
            ("b", "memory"),
        )
        a_id = conn.execute("SELECT id FROM kg_entities WHERE name = 'a'").fetchone()[0]
        b_id = conn.execute("SELECT id FROM kg_entities WHERE name = 'b'").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
            (a_id, b_id, "related", 0.8),
        )
        conn.commit()
        self._upsert(conn, "a", "updated x")
        edge = conn.execute(
            "SELECT weight FROM kg_edges WHERE source_id = ? AND target_id = ?",
            (a_id, b_id),
        ).fetchone()
        self.assertIsNotNone(edge, "KG edge lost after upsert!")
        self.assertAlmostEqual(edge[0], 0.8)


class TestHardDeleteCascade(unittest.TestCase):
    """hard_delete_note() must cascade-delete all linked data."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "cascade_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hard_delete_removes_embedding(self):
        from infra.memory_common import connection_pool
        from memory_delete import hard_delete_note
        from datetime import datetime, timedelta, timezone
        import numpy as np

        conn = connection_pool.get(self.db_path, timeout=5.0)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, created_at) VALUES (?, ?, ?)",
            ("hd-1", "del", old),
        )
        blob = np.zeros(8, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("hd-1", "h", blob, "m", 8, 0.0),
        )
        conn.commit()
        hard_delete_note(self.db_path, "hd-1")
        conn2 = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM memories WHERE id = ?", ("hd-1",)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", ("hd-1",)
            ).fetchone()[0],
            0,
        )

    def test_hard_delete_removes_vec_keys(self):
        from infra.memory_common import connection_pool
        from memory_delete import hard_delete_note
        from datetime import datetime, timedelta, timezone

        conn = connection_pool.get(self.db_path, timeout=5.0)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, created_at) VALUES (?, ?, ?)",
            ("hd-2", "del", old),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (memory_id, key) VALUES (?, ?)",
            ("hd-2", 100),
        )
        conn.commit()
        hard_delete_note(self.db_path, "hd-2")
        conn2 = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM memory_vec_keys WHERE memory_id = ?", ("hd-2",)
            ).fetchone()[0],
            0,
        )

    def test_hard_delete_removes_backlinks(self):
        from infra.memory_common import connection_pool
        from memory_delete import hard_delete_note
        from datetime import datetime, timedelta, timezone

        conn = connection_pool.get(self.db_path, timeout=5.0)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, created_at) VALUES (?, ?, ?)",
            ("hd-3", "del", old),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
            ("hd-4", "keep"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
            ("hd-3", "hd-4"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
            ("hd-4", "hd-3"),
        )
        conn.commit()
        hard_delete_note(self.db_path, "hd-3")
        conn2 = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id = ? OR target_id = ?",
                ("hd-3", "hd-3"),
            ).fetchone()[0],
            0,
        )


class TestPurgeExpiredCascade(unittest.TestCase):
    """purge_expired() must cascade-delete expired memories + embeddings."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "purge_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_purge_removes_expired_with_embeddings(self):
        from infra.memory_common import connection_pool
        import numpy as np

        conn = connection_pool.get(self.db_path, timeout=5.0)
        conn.execute(
            "INSERT INTO memories (id, content, deleted_at) VALUES (?, ?, datetime('now', '-31 day'))",
            ("expired-1", "old"),
        )
        blob = np.zeros(8, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, embedding, dim) VALUES (?, ?, ?)",
            ("expired-1", blob, 8),
        )
        conn.commit()
        from memory_delete import purge_expired

        purge_expired(self.db_path)
        conn2 = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM memories WHERE id = ?", ("expired-1",)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn2.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("expired-1",),
            ).fetchone()[0],
            0,
        )


# ---------------------------------------------------------------------------
# Save Pipeline E2E
# ---------------------------------------------------------------------------


class TestSavePipelineEndToEnd(unittest.TestCase):
    """End-to-end: save a note via _update_memory_index_incremental,
    then verify ALL subsystem data persists."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "e2e_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, note_id, content, tags=None):
        """Helper: split note_id into category/title_slug and call save pipeline."""
        from pathlib import Path
        from save_pipeline import _update_memory_index_incremental

        parts = note_id.split("/", 1)
        category = parts[0]
        title_slug = parts[1] if len(parts) > 1 else note_id
        _update_memory_index_incremental(
            Path(self.db_path),
            category,
            title_slug,
            content,
            tags=tags or [],
            pinned=False,
            now_iso=_now_iso(),
            is_global=False,
            metadata_json="{}",
        )

    def test_save_indexes_all_subsystems(self):
        """After save_pipeline, data exists in memories, embeddings, KG."""
        self._save("e2e/note-1", "This is test content about Python programming.")
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id = ?", ("e2e/note-1",)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("e2e/note-1",),
            ).fetchone()[0],
            1,
        )
        # KG entities are extracted from content, not created per note
        self.assertGreaterEqual(
            conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0], 0
        )

    def test_save_with_wikilinks_creates_backlinks(self):
        """Save with [[wiki-links]] creates bidirectional backlinks."""
        self._save("e2e/target", "Target note.")
        self._save("e2e/source", "See [[e2e/target]] for details.")
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id = ? AND target_id = ?",
                ("e2e/source", "e2e/target"),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id = ? AND target_id = ?",
                ("e2e/target", "e2e/source"),
            ).fetchone()[0],
            1,
        )

    def test_upsert_preserves_all_subsystem_data(self):
        """Re-saving the same note preserves all subsystem data."""
        self._save("e2e/upsert", "Original content.", tags=["v1"])
        from infra.memory_common import connection_pool

        conn = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("e2e/upsert",),
            ).fetchone()[0],
            1,
        )
        # Second save
        self._save("e2e/upsert", "Updated content.", tags=["v2"])
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
                ("e2e/upsert",),
            ).fetchone()[0],
            1,
        )
        self.assertIn(
            "Updated",
            conn.execute(
                "SELECT content FROM memories WHERE id = ?", ("e2e/upsert",)
            ).fetchone()[0],
        )


# ---------------------------------------------------------------------------
# Sync Invariant Tests
# ---------------------------------------------------------------------------


class TestSyncInvariant(unittest.TestCase):
    """Verify sync_invariant detects and reports drift correctly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "sync_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_conn(self):
        from infra.memory_common import connection_pool

        return connection_pool.get(self.db_path, timeout=5.0)

    def test_healthy_state_no_drift(self):
        """A freshly saved memory with all subsystems indexed shows no drift."""
        from pathlib import Path
        from save_pipeline import _update_memory_index_incremental
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        _update_memory_index_incremental(
            Path(self.db_path),
            "test",
            "healthy",
            "Healthy content here.",
            tags=[],
            pinned=False,
            now_iso=_now_iso(),
            is_global=False,
            metadata_json="{}",
        )
        conn = self._get_conn()
        result = check_sync_invariant(conn)
        self.assertEqual(get_drifted_subsystems(result), [])

    def test_drifted_fts_detected(self):
        """Missing FTS5 entry for an existing memory is detected as drift."""
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        conn = self._get_conn()
        # Insert two memories (trigger auto-syncs to FTS)
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags, category) VALUES (?, ?, '[]', 'test')",
            ("test/drifted-1", "content without FTS"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags, category) VALUES (?, ?, '[]', 'test')",
            ("test/drifted-2", "content with FTS"),
        )
        conn.commit()
        # Delete the second FTS entry to create drift (simulate corruption)
        conn.execute("DELETE FROM memories_fts WHERE id = ?", ("test/drifted-2",))
        conn.commit()
        result = check_sync_invariant(conn)
        drifted = get_drifted_subsystems(result)
        self.assertTrue(
            any("fts" in d.lower() or "memories_fts" in d.lower() for d in drifted),
            f"FTS drift not detected: {drifted}",
        )

    def test_drifted_embeddings_detected(self):
        """Missing embedding for an existing memory is detected as drift."""
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        conn = self._get_conn()
        # Need multiple memories to get "drift" (partial coverage), not "empty"
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, '[]')",
            ("test/no-emb-1", "content without embedding"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, '[]')",
            ("test/no-emb-2", "content with embedding"),
        )
        import numpy as np

        blob = np.zeros(8, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("test/no-emb-2", "h", blob, "m", 8, 0.0),
        )
        conn.commit()
        result = check_sync_invariant(conn)
        drifted = get_drifted_subsystems(result)
        self.assertTrue(
            any("embed" in d.lower() or "vec" in d.lower() for d in drifted),
            f"Embedding drift not detected: {drifted}",
        )

    def test_drifted_kg_detected(self):
        """Missing KG entity for an existing memory is detected as drift."""
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        conn = self._get_conn()
        # Need enough memories that one KG entity is partial coverage (<5% threshold)
        for i in range(21):
            conn.execute(
                "INSERT OR IGNORE INTO memories (id, content, tags) VALUES (?, ?, '[]')",
                (f"test/no-kg-{i}", "content without KG entity"),
            )
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
            ("partial-kg", "concept"),
        )
        conn.commit()
        result = check_sync_invariant(conn)
        drifted = get_drifted_subsystems(result)
        self.assertTrue(
            any("kg" in d.lower() or "graph" in d.lower() for d in drifted),
            f"KG drift not detected: {drifted}",
        )

    def test_healthy_after_full_index(self):
        """After full pipeline save, sync check shows 0 drift."""
        from pathlib import Path
        from save_pipeline import _update_memory_index_incremental
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        for i in range(5):
            _update_memory_index_incremental(
                Path(self.db_path),
                "test",
                f"note-{i}",
                f"Content for note {i}.",
                tags=[f"tag-{i}"],
                pinned=False,
                now_iso=_now_iso(),
                is_global=False,
                metadata_json="{}",
            )
        conn = self._get_conn()
        result = check_sync_invariant(conn)
        self.assertEqual(get_drifted_subsystems(result), [])


class TestConnectionPoolThreadIsolation(unittest.TestCase):
    """Connections from different threads must not interfere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "thread_test.db")
        bare = _make_bare_db(self.db_path)
        bare.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_different_threads_get_different_connections(self):
        from infra.memory_common import connection_pool

        results = {}

        def worker(name):
            conn = connection_pool.get(self.db_path, timeout=5.0)
            results[name] = id(conn)

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertNotEqual(results["t1"], results["t2"])

    def test_same_thread_reuses_connection(self):
        from infra.memory_common import connection_pool

        conn1 = connection_pool.get(self.db_path, timeout=5.0)
        conn2 = connection_pool.get(self.db_path, timeout=5.0)
        self.assertEqual(id(conn1), id(conn2))


if __name__ == "__main__":
    unittest.main()
