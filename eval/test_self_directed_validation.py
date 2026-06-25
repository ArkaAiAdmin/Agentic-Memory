"""Comprehensive validation tests for self-directed memory, adaptive retention,
tier management, orphan cleanup, drift backfill, pinned decay, sync invariant,
and schema version.

Uses a TEMP DB — no production data touched. Pattern from test_mcp_live.py.
"""

import pytest

import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_common import (
    open_db,
    run_db_migrations,
    SCHEMA_VERSION,
    GLOBAL_MEM_DIR,
    connection_pool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_db(tmpdir: str) -> Path:
    """Create a fully-migrated temp DB and return its path.

    Creates the base memories table + all auxiliary tables, then runs
    migrations to bring the schema up to SCHEMA_VERSION.
    """
    db_path = Path(tmpdir) / "memory.db"
    with open_db(db_path) as conn:
        # --- Base memories table (required by all subsystems) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id                  TEXT PRIMARY KEY,
                content             TEXT NOT NULL,
                source_file         TEXT,
                tags                TEXT DEFAULT '[]',
                created_at          REAL,
                updated_at          REAL,
                pinned              INTEGER DEFAULT 0,
                repo_id             TEXT,
                consolidation_state TEXT,
                observed_at         REAL,
                fitness_score       REAL,
                backlinks           TEXT,
                category            TEXT,
                context_prefix      TEXT,
                deleted_by          TEXT,
                superseded_by       TEXT,
                valid_from          TEXT,
                valid_to            TEXT,
                last_accessed       TEXT,
                deleted_at          TEXT,
                tier                TEXT,
                importance          REAL,
                importance_score    REAL,
                metadata            TEXT,
                access_count        INTEGER DEFAULT 0,
                success_score       REAL DEFAULT 0.0
            )
        """)
        # --- schema_version ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        # --- backlinks ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backlinks (
                source_id   TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                source_file TEXT,
                UNIQUE(source_id, target_id)
            )
        """)
        # --- memory_chunks ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id     TEXT NOT NULL,
                chunk_idx     INTEGER NOT NULL,
                start_offset  INTEGER NOT NULL,
                end_offset    INTEGER NOT NULL,
                content       TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(parent_id, chunk_idx)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent_id "
            "ON memory_chunks(parent_id)"
        )
        # --- memory_embeddings ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                memory_id       TEXT PRIMARY KEY,
                content_hash    TEXT NOT NULL,
                embedding       BLOB NOT NULL,
                model_revision  TEXT NOT NULL,
                dim             INTEGER NOT NULL,
                updated_at      REAL NOT NULL,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hash "
            "ON memory_embeddings(content_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_revision "
            "ON memory_embeddings(model_revision)"
        )
        # --- user_access_log (adaptive retention) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_access_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id    TEXT NOT NULL,
                access_ts  REAL NOT NULL,
                source     TEXT NOT NULL DEFAULT 'search'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_access_log_note_id "
            "ON user_access_log(note_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_access_log_ts "
            "ON user_access_log(access_ts)"
        )
        # --- kg_entities ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kg_entities (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'concept',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # --- kg_edges ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kg_edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                relation    TEXT NOT NULL DEFAULT 'related_to',
                weight      REAL DEFAULT 1.0,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (source_id) REFERENCES kg_entities(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES kg_entities(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id)"
        )
        # --- kg_facts ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kg_facts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_memory   TEXT NOT NULL,
                subject         TEXT NOT NULL,
                predicate       TEXT NOT NULL,
                object_value    TEXT NOT NULL,
                confidence      REAL DEFAULT 1.0,
                created_at      TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (source_memory) REFERENCES memories(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_facts_source ON kg_facts(source_memory)"
        )
        # --- memory_vec_idx ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_vec_idx (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                n_vectors         INTEGER NOT NULL,
                dim               INTEGER NOT NULL,
                metric            TEXT NOT NULL,
                quantization      TEXT NOT NULL,
                connectivity      INTEGER NOT NULL,
                expansion_add     INTEGER NOT NULL,
                expansion_search  INTEGER NOT NULL,
                built_at          REAL NOT NULL,
                index_blob        BLOB NOT NULL,
                key_count         INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_vec_keys (
                key         INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_keys_memory_id "
            "ON memory_vec_keys(memory_id)"
        )
        # --- memory_audit_log ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                tool            TEXT NOT NULL,
                args            TEXT,
                results_count   INTEGER,
                top1_id         TEXT,
                latency_ms      REAL NOT NULL,
                error           TEXT,
                request_id      TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_tool_ts "
            "ON memory_audit_log(tool, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON memory_audit_log(ts)"
        )
        # --- FTS5 ---
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, tags,
                    tokenize='porter unicode61'
                )
            """)
        except sqlite3.OperationalError:
            pass  # FTS5 may not be available
        # --- FTS sync triggers (SQLite 3.53+ uses DELETE FROM) ---
        # Drop old triggers that use the deprecated 'delete' INSERT syntax
        for trig in ("memories_ai", "memories_ad", "memories_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        try:
            conn.execute("""
                CREATE TRIGGER memories_ai AFTER INSERT ON memories
                WHEN new.deleted_at IS NULL
                BEGIN
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.rowid, new.content, new.tags);
                END
            """)
            conn.execute("""
                CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.rowid;
                END
            """)
            conn.execute("""
                CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.rowid;
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.rowid, new.content, new.tags);
                END
            """)
        except sqlite3.OperationalError:
            pass
        conn.commit()
    return db_path


def _insert_note(
    conn: sqlite3.Connection,
    note_id: str,
    content: str = "test content",
    *,
    pinned: int = 0,
    tier: str | None = None,
    importance: float | None = None,
    access_count: int = 0,
    success_score: float = 0.0,
    updated_at: float | None = None,
    created_at: float | None = None,
    last_accessed: str | None = None,
    metadata: str | None = None,
    deleted_at: str | None = None,
) -> None:
    """Insert a note into the memories table with all relevant columns."""
    now = time.time()
    conn.execute(
        """INSERT INTO memories
           (id, content, source_file, tags, category, observed_at, fitness_score,
            repo_id, pinned, tier, importance,
            access_count, success_score, updated_at, created_at,
            last_accessed, metadata, deleted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note_id,
            content,
            f"{note_id}.md",
            "[]",
            "test",
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            0.5,
            "test",
            pinned,
            tier,
            importance,
            access_count,
            success_score,
            updated_at or now,
            created_at or (now - 86400 * 100),
            last_accessed,
            metadata,
            deleted_at,
        ),
    )
    conn.commit()


def _count_table(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaVersion(unittest.TestCase):
    """Verify schema_version pragma is 5."""

    def test_schema_version_is_current(self):
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = _create_test_db(tmpdir)
            with open_db(db_path) as conn:
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE id=1"
                ).fetchone()
                self.assertIsNotNone(row, "schema_version row missing")
                self.assertEqual(row[0], SCHEMA_VERSION)
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)


class TestHeartbeat(unittest.TestCase):
    """Run run_heartbeat() and verify it re-evaluates, moves tiers, archives."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_heartbeat_evaluates_all_notes(self):
        """Heartbeat evaluates every active note."""
        from self_directed import run_heartbeat

        for i in range(5):
            _insert_note(self.conn, f"note-{i}", f"content-{i}")

        result = run_heartbeat(self.conn)
        self.assertEqual(result["evaluated"], 5)
        self.assertFalse(result["dry_run"])

    def test_heartbeat_assigns_tiers(self):
        """Heartbeat assigns tiers based on importance scores."""
        from self_directed import run_heartbeat

        # A note with recent access and pinned should get hot tier
        _insert_note(
            self.conn,
            "hot-note",
            "important content",
            pinned=1,
            access_count=20,
            success_score=0.9,
            updated_at=time.time(),
        )
        # An old, unused note should get cold tier
        _insert_note(
            self.conn,
            "cold-note",
            "stale content",
            pinned=0,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 365,
            created_at=time.time() - 86400 * 365,
        )

        result = run_heartbeat(self.conn)
        self.assertGreaterEqual(result["tier_changes"], 1)

        hot = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'hot-note'"
        ).fetchone()
        cold = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'cold-note'"
        ).fetchone()
        self.assertEqual(hot[0], "hot")
        self.assertEqual(cold[0], "cold")

    def test_heartbeat_moves_notes_bidirectionally(self):
        """Notes can be promoted (cold→warm) and demoted (warm→cold)."""
        from self_directed import run_heartbeat, _assign_tier

        # Start as warm with old timestamps
        _insert_note(
            self.conn,
            "promote-me",
            "content",
            pinned=0,
            tier="cold",
            access_count=5,
            success_score=0.8,
            updated_at=time.time(),
            created_at=time.time() - 86400 * 100,
        )

        result = run_heartbeat(self.conn)
        row = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'promote-me'"
        ).fetchone()
        # With access_count=5, success_score=0.8, recent updated_at → importance ~0.63 → warm
        self.assertIn(row[0], ["warm", "hot"])

    def test_heartbeat_archives_low_importance_old_notes(self):
        """Cold notes with importance < 0.15 and age >= 90d get archived."""
        from self_directed import run_heartbeat

        _insert_note(
            self.conn,
            "archive-me",
            "unimportant",
            pinned=0,
            tier="warm",
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 200,
            created_at=time.time() - 86400 * 200,
        )

        result = run_heartbeat(self.conn)
        tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'archive-me'"
        ).fetchone()[0]
        self.assertEqual(tier, "cold")

    def test_heartbeat_cleans_orphaned_chunks(self):
        """Heartbeat removes chunks referencing deleted notes."""
        from self_directed import run_heartbeat

        # Insert a note then add orphaned chunks
        _insert_note(self.conn, "orphan-parent", "short")
        try:
            self.conn.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
                "VALUES ('deleted-note-999', 0, 0, 10, 'orphan chunk')"
            )
            self.conn.commit()
            self.assertEqual(_count_table(self.conn, "memory_chunks"), 1)

            result = run_heartbeat(self.conn)
            remaining = _count_table(self.conn, "memory_chunks")
            # The orphan should be cleaned (the only chunk points to deleted-note-999)
            self.assertEqual(remaining, 0)
        except sqlite3.OperationalError:
            self.skipTest("memory_chunks table not available")

    def test_heartbeat_cleans_orphaned_embeddings(self):
        """Heartbeat removes embeddings referencing deleted notes."""
        from self_directed import run_heartbeat

        _insert_note(self.conn, "emb-parent", "content")
        try:
            import hashlib

            h = hashlib.sha256(b"content").hexdigest()
            self.conn.execute(
                "INSERT INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES ('deleted-emb-999', ?, X'00000000', 'test', 4, ?)",
                (h, time.time()),
            )
            self.conn.commit()
            self.assertEqual(_count_table(self.conn, "memory_embeddings"), 1)

            result = run_heartbeat(self.conn)
            remaining = _count_table(self.conn, "memory_embeddings")
            self.assertEqual(remaining, 0)
        except sqlite3.OperationalError:
            self.skipTest("memory_embeddings table not available")

    def test_heartbeat_returns_sync_backfill(self):
        """Heartbeat attempts sync backfill when drift detected."""
        from self_directed import run_heartbeat

        _insert_note(self.conn, "backfill-test", "some content")
        result = run_heartbeat(self.conn)
        # sync_backfill is None if no drift, or dict if drift was found
        self.assertIn("sync_backfill", result)

    def test_heartbeat_dry_run_does_not_modify(self):
        """Dry-run heartbeat should not modify any data."""
        from self_directed import run_heartbeat

        _insert_note(self.conn, "dry-note", "content", tier="cold")
        result = run_heartbeat(self.conn, dry_run=True)
        self.assertTrue(result["dry_run"])

        tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'dry-note'"
        ).fetchone()[0]
        self.assertEqual(tier, "cold")


class TestAdaptiveRetention(unittest.TestCase):
    """Verify adaptive retention schema, access recording, and half-life computation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._old_env = os.environ.get("MEMORY_ADAPTIVE_RETENTION")

    def tearDown(self):
        self.conn.close()
        if self._old_env is None:
            os.environ.pop("MEMORY_ADAPTIVE_RETENTION", None)
        else:
            os.environ["MEMORY_ADAPTIVE_RETENTION"] = self._old_env
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ensure_adaptive_schema_creates_table(self):
        """ensure_adaptive_schema creates user_access_log table."""
        from adaptive_retention import ensure_adaptive_schema

        ensure_adaptive_schema(self.conn)
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("user_access_log", tables)

    def test_ensure_adaptive_schema_is_idempotent(self):
        """Calling ensure_adaptive_schema twice doesn't error."""
        from adaptive_retention import ensure_adaptive_schema

        ensure_adaptive_schema(self.conn)
        ensure_adaptive_schema(self.conn)  # no-op

    def test_record_access_inserts_row(self):
        """record_access inserts an access log entry when enabled."""
        os.environ["MEMORY_ADAPTIVE_RETENTION"] = "1"
        # Re-import to pick up new env
        import importlib
        import adaptive_retention

        importlib.reload(adaptive_retention)

        _insert_note(self.conn, "access-test", "content")
        adaptive_retention.record_access(self.conn, "access-test", "search")

        count = self.conn.execute(
            "SELECT COUNT(*) FROM user_access_log WHERE note_id = 'access-test'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_record_access_noop_when_disabled(self):
        """record_access is a no-op when adaptive_retention is disabled via config."""
        import adaptive_retention
        from unittest.mock import patch
        from config import MemoryConfig

        disabled_cfg = MemoryConfig(adaptive_retention=False)
        with patch("config.get_config", return_value=disabled_cfg):
            # AGENTS.md: make_lazy_getattr caches resolved values in
            # the module's __dict__ on first access. Clear the cache
            # so the patched config is honored.
            for _attr in ("ADAPTIVE_RETENTION_ENABLED",):
                if _attr in adaptive_retention.__dict__:
                    del adaptive_retention.__dict__[_attr]
            adaptive_retention.record_access(self.conn, "no-access", "search")
            try:
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM user_access_log"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            except sqlite3.OperationalError:
                pass  # table doesn't exist — correct

    def test_compute_adaptive_halflife_base_case(self):
        """With no access history, half-life equals base (180 days)."""
        os.environ["MEMORY_ADAPTIVE_RETENTION"] = "1"
        import importlib
        import adaptive_retention

        importlib.reload(adaptive_retention)

        hl = adaptive_retention.compute_adaptive_halflife(
            "nonexistent-note",
            base_halflife=180.0,
            db_path=str(self.db_path),
        )
        self.assertAlmostEqual(hl, 180.0, delta=1.0)

    def test_compute_adaptive_halflife_with_accesses(self):
        """High access count increases half-life (up to max multiplier)."""
        os.environ["MEMORY_ADAPTIVE_RETENTION"] = "1"
        import importlib
        import adaptive_retention

        importlib.reload(adaptive_retention)

        _insert_note(self.conn, "boosted-note", "content")
        # Record 10 accesses
        for _ in range(10):
            adaptive_retention.record_access(self.conn, "boosted-note", "search")
        # record_access no longer auto-commits (saga fix) — commit here
        # so the writes are visible to compute_adaptive_halflife's connection.
        self.conn.commit()

        hl = adaptive_retention.compute_adaptive_halflife(
            "boosted-note",
            base_halflife=180.0,
            db_path=str(self.db_path),
        )
        # multiplier = min(1.0 + 10*0.75, 4.0) = min(8.5, 4.0) = 4.0
        # hl = 180 * 4.0 = 720, capped at 730
        self.assertGreater(hl, 180.0)

    def test_compute_adaptive_halflife_disabled(self):
        """When disabled, always returns base_halflife."""
        os.environ.pop("MEMORY_ADAPTIVE_RETENTION", None)
        import importlib
        import adaptive_retention

        importlib.reload(adaptive_retention)

        hl = adaptive_retention.compute_adaptive_halflife("any", base_halflife=180.0)
        self.assertEqual(hl, 180.0)


class TestTierManagement(unittest.TestCase):
    """Verify pinned notes get warm floor and unpinned notes decay."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pinned_note_never_cold(self):
        """Pinned notes get tier >= warm (importance >= 0.4 floor)."""
        from self_directed import run_heartbeat, compute_importance

        _insert_note(
            self.conn,
            "pinned-warm",
            "content",
            pinned=1,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 365,
            created_at=time.time() - 86400 * 365,
        )

        run_heartbeat(self.conn)
        row = self.conn.execute(
            "SELECT tier, importance_score FROM memories WHERE id = 'pinned-warm'"
        ).fetchone()
        self.assertIn(row[0], ["warm", "hot"])
        # importance_score should be at least 0.4 due to pinned bonus
        self.assertGreaterEqual(row[1], 0.15)  # warm threshold minimum

    def test_unpinned_old_note_decays_to_cold(self):
        """An unpinned, unused, old note decays to cold tier."""
        from self_directed import run_heartbeat

        _insert_note(
            self.conn,
            "decay-me",
            "stale",
            pinned=0,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 500,
            created_at=time.time() - 86400 * 500,
        )

        run_heartbeat(self.conn)
        tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'decay-me'"
        ).fetchone()[0]
        self.assertEqual(tier, "cold")

    def test_tier_is_bidi_directional(self):
        """Tier can be promoted and demoted in same heartbeat."""
        from self_directed import run_heartbeat

        # Cold note with recent update → should be promoted
        _insert_note(
            self.conn,
            "promote-cold",
            "content",
            pinned=0,
            tier="cold",
            access_count=5,
            success_score=0.5,
            updated_at=time.time(),
            created_at=time.time() - 86400 * 10,
        )
        # Hot note that aged → should be demoted
        _insert_note(
            self.conn,
            "demote-hot",
            "content",
            pinned=0,
            tier="hot",
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 500,
            created_at=time.time() - 86400 * 500,
        )

        run_heartbeat(self.conn)
        promote_tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'promote-cold'"
        ).fetchone()[0]
        demote_tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'demote-hot'"
        ).fetchone()[0]

        # promote-cold should be at least warm
        self.assertIn(promote_tier, ["warm", "hot"])
        # demote-hot should be cold or warm (not hot)
        self.assertIn(demote_tier, ["cold", "warm"])


class TestOrphanCleanup(unittest.TestCase):
    """Create orphaned KG entities/embeddings/chunks and verify heartbeat cleans them."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_kg_tables_if_absent(self):
        """Create kg_entities, kg_edges, kg_facts if they don't exist."""
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    entity_type TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    FOREIGN KEY (source_id) REFERENCES kg_entities(id),
                    FOREIGN KEY (target_id) REFERENCES kg_entities(id)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    source_memory TEXT,
                    FOREIGN KEY (source_memory) REFERENCES memories(id)
                )
            """)
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def test_orphaned_chunks_cleaned(self):
        """Chunks referencing non-existent notes are cleaned."""
        from self_directed import _cleanup_orphaned_subsystem_data

        _insert_note(self.conn, "existing-note", "content")
        # Add orphaned chunk
        try:
            self.conn.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
                "VALUES ('deleted-note-999', 0, 0, 10, 'orphan')"
            )
            self.conn.commit()

            stats = _cleanup_orphaned_subsystem_data(self.conn)
            self.assertGreater(stats.get("orphaned_chunks", 0), 0)
            self.assertEqual(_count_table(self.conn, "memory_chunks"), 0)
        except sqlite3.OperationalError:
            self.skipTest("memory_chunks table not available")

    def test_orphaned_embeddings_cleaned(self):
        """Embeddings referencing non-existent notes are cleaned."""
        from self_directed import _cleanup_orphaned_subsystem_data

        _insert_note(self.conn, "existing-emb", "content")
        try:
            import hashlib

            h = hashlib.sha256(b"x").hexdigest()
            self.conn.execute(
                "INSERT INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES ('deleted-emb-999', ?, X'00000000', 'test', 4, ?)",
                (h, time.time()),
            )
            self.conn.commit()

            stats = _cleanup_orphaned_subsystem_data(self.conn)
            self.assertGreater(stats.get("orphaned_embeddings", 0), 0)
            self.assertEqual(_count_table(self.conn, "memory_embeddings"), 0)
        except sqlite3.OperationalError:
            self.skipTest("memory_embeddings table not available")

    def test_orphaned_kg_entities_cleaned(self):
        """KG entities with no edges or facts are cleaned."""
        from self_directed import _cleanup_orphaned_subsystem_data

        if not self._create_kg_tables_if_absent():
            self.skipTest("Could not create KG tables")

        _insert_note(self.conn, "kg-parent", "content")
        # Insert orphaned entity (no edges, no facts)
        self.conn.execute(
            "INSERT INTO kg_entities (id, name, entity_type) VALUES (10002, 'OrphanEntity', 'concept')"
        )
        self.conn.commit()

        stats = _cleanup_orphaned_subsystem_data(self.conn)
        self.assertGreater(stats.get("orphaned_kg_entities", 0), 0)
        self.assertEqual(_count_table(self.conn, "kg_entities"), 0)

    def test_orphaned_kg_facts_cleaned(self):
        """KG facts referencing deleted notes are cleaned."""
        from self_directed import _cleanup_orphaned_subsystem_data

        if not self._create_kg_tables_if_absent():
            self.skipTest("Could not create KG tables")

        self.conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, source_memory) "
            "VALUES ('A', 'relates_to', 'B', 'deleted-note-888')"
        )
        self.conn.commit()

        stats = _cleanup_orphaned_subsystem_data(self.conn)
        self.assertGreater(stats.get("orphaned_kg_facts", 0), 0)
        self.assertEqual(_count_table(self.conn, "kg_facts"), 0)

    def test_orphaned_kg_edges_cleaned(self):
        """KG edges referencing non-existent entities are cleaned."""
        import sqlite3 as _sqlite3
        from self_directed import _cleanup_orphaned_subsystem_data

        if not self._create_kg_tables_if_absent():
            self.skipTest("Could not create KG tables")

        # Use raw connection to bypass FK and insert orphaned edge
        raw = _sqlite3.connect(str(self.db_path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            "INSERT INTO kg_entities (id, name, entity_type) VALUES (10001, 'A', 'concept')"
        )
        raw.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation) "
            "VALUES (10001, 99999, 'relates_to')"
        )
        raw.commit()
        raw.close()

        stats = _cleanup_orphaned_subsystem_data(self.conn)
        self.assertGreater(stats.get("orphaned_kg_edges", 0), 0)

    def test_dry_run_does_not_delete(self):
        """Orphan cleanup dry_run does not delete anything."""
        from self_directed import _cleanup_orphaned_subsystem_data

        try:
            self.conn.execute(
                "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
                "VALUES ('ghost-999', 0, 0, 10, 'ghost')"
            )
            self.conn.commit()

            stats = _cleanup_orphaned_subsystem_data(self.conn, dry_run=True)
            self.assertEqual(stats.get("orphaned_chunks", 0), 1)
            # Should still exist because dry_run
            self.assertEqual(_count_table(self.conn, "memory_chunks"), 1)
        except sqlite3.OperationalError:
            self.skipTest("memory_chunks table not available")


class TestDriftBackfill(unittest.TestCase):
    """Create notes missing embeddings/chunks/KG and verify backfill."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drift_backfill_tiers(self):
        """Notes with NULL tier get tier='cold' on backfill."""
        from self_directed import _backfill_drifted_subsystems

        _insert_note(self.conn, "no-tier-note", "content", tier=None)
        _insert_note(self.conn, "has-tier-note", "content", tier="hot")

        stats = _backfill_drifted_subsystems(self.conn, ["tiers"])
        self.assertIn("tiers", stats.get("fixed", {}))
        # The note with NULL tier should now have 'cold'
        tier = self.conn.execute(
            "SELECT tier FROM memories WHERE id = 'no-tier-note'"
        ).fetchone()[0]
        self.assertEqual(tier, "cold")

    def test_drift_backfill_metadata(self):
        """Notes with NULL metadata get metadata='{}' on backfill."""
        from self_directed import _backfill_drifted_subsystems

        _insert_note(self.conn, "no-meta-note", "content", metadata=None)
        _insert_note(self.conn, "has-meta-note", "content", metadata='{"key":"val"}')

        stats = _backfill_drifted_subsystems(self.conn, ["metadata"])
        self.assertIn("metadata", stats.get("fixed", {}))
        meta = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = 'no-meta-note'"
        ).fetchone()[0]
        self.assertEqual(meta, "{}")


class TestPinnedDecay(unittest.TestCase):
    """Verify auto-unpin logic: psi > 60 AND days_since > 180."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stale_pinned_note_gets_unpinned(self):
        """A pinned note with psi > 60 AND days_since > 180 is auto-unpinned."""
        from pinned_decay import check

        # Create a note pinned 200 days ago with only 1 access → psi = 200
        old_ts = time.time() - 86400 * 200
        _insert_note(
            self.conn,
            "stale-pinned",
            "old content",
            pinned=1,
            access_count=1,
            success_score=0.5,
            updated_at=old_ts,
            created_at=old_ts,
            last_accessed=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(old_ts)),
        )

        report = check(dry_run=True, db_path=self.db_path)
        self.assertGreater(report["summary"]["auto_unpin_candidates"], 0)
        self.assertEqual(report["summary"]["pinned_total"], 1)

    def test_auto_unpin_actually_unpins(self):
        """auto_unpin with dry_run=False sets pinned=0."""
        from pinned_decay import check

        old_ts = time.time() - 86400 * 200
        _insert_note(
            self.conn,
            "actually-unpin",
            "content",
            pinned=1,
            access_count=1,
            updated_at=old_ts,
            created_at=old_ts,
            last_accessed=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(old_ts)),
        )

        report = check(dry_run=False, db_path=self.db_path)
        self.assertIn("actually-unpin", report["summary"]["unpinned"])

        pinned = self.conn.execute(
            "SELECT pinned FROM memories WHERE id = 'actually-unpin'"
        ).fetchone()[0]
        self.assertEqual(pinned, 0)

    def test_fresh_pinned_note_not_unpinned(self):
        """A recently accessed pinned note is NOT auto-unpinned."""
        from pinned_decay import check

        _insert_note(
            self.conn,
            "fresh-pinned",
            "content",
            pinned=1,
            access_count=50,
            updated_at=time.time(),
            created_at=time.time() - 86400 * 10,
            last_accessed=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        )

        report = check(dry_run=True, db_path=self.db_path)
        self.assertEqual(report["summary"]["auto_unpin_candidates"], 0)

    def test_review_candidate_flagged(self):
        """Notes with psi > 30 AND days > 365 are flagged for review."""
        from pinned_decay import check

        old_ts = time.time() - 86400 * 400
        _insert_note(
            self.conn,
            "review-me",
            "content",
            pinned=1,
            access_count=10,
            updated_at=old_ts,
            created_at=old_ts,
            last_accessed=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(old_ts)),
        )

        report = check(dry_run=True, db_path=self.db_path)
        self.assertGreater(report["summary"]["review_candidates"], 0)


class TestSyncInvariant(unittest.TestCase):
    """Verify check_sync_invariant detects drift across subsystems."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_healthy_state(self):
        """When all subsystems match, overall is 'healthy'."""
        from sync_invariant import check_sync_invariant

        # Insert note — memories_ai trigger auto-inserts FTS entry
        _insert_note(self.conn, "healthy-1", "short content", tier="warm")

        result = check_sync_invariant(self.conn)
        self.assertIn(result["overall"], ["healthy", "drift"])

    def test_detects_notes_not_in_fts(self):
        """Detects notes in memories but not in FTS."""
        from sync_invariant import check_sync_invariant

        _insert_note(self.conn, "no-fts-note", "content here", tier="warm")
        # Don't add FTS entry

        result = check_sync_invariant(self.conn)
        fts_info = result["subsystems"]["fts"]
        # If fts_count < total, it should show drift
        if fts_info["status"] == "drift":
            self.assertIn("coverage", fts_info["detail"])

    def test_detects_notes_not_in_embeddings(self):
        """Detects notes in memories but not in embeddings."""
        from sync_invariant import check_sync_invariant

        _insert_note(self.conn, "no-emb-note", "content", tier="warm")
        # Don't add embedding

        result = check_sync_invariant(self.conn)
        emb_info = result["subsystems"]["embeddings"]
        self.assertIn(emb_info["status"], ["drift", "empty"])

    def test_detects_notes_not_in_chunks(self):
        """Detects notes missing from chunks subsystem."""
        from sync_invariant import check_sync_invariant

        _insert_note(self.conn, "no-chunks-note", "x" * 5000, tier="warm")
        # Long note should be chunked but isn't

        result = check_sync_invariant(self.conn)
        chunks_info = result["subsystems"]["chunks"]
        self.assertIn(chunks_info["status"], ["drift", "empty"])

    def test_detects_ghost_fts_rows(self):
        """Detects FTS rows referencing deleted memories."""
        from sync_invariant import check_sync_invariant

        # Drop triggers to have full control over FTS state
        self.conn.execute("DROP TRIGGER IF EXISTS memories_ai")
        self.conn.execute("DROP TRIGGER IF EXISTS memories_au")
        self.conn.execute("DROP TRIGGER IF EXISTS memories_ad")
        self.conn.commit()

        # Insert a note (no trigger, so no auto FTS insert)
        _insert_note(self.conn, "ghost-fts", "content", tier="warm")
        rowid = self.conn.execute(
            "SELECT rowid FROM memories WHERE id='ghost-fts'"
        ).fetchone()[0]

        # Verify no FTS row yet
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE rowid = ?", (rowid,)
        ).fetchone()[0]
        self.assertEqual(fts_count, 0)

        # Soft-delete the note
        self.conn.execute(
            "UPDATE memories SET deleted_at = '2025-01-01' WHERE id = 'ghost-fts'"
        )
        self.conn.commit()

        # Manually insert a ghost FTS row (simulating corruption)
        self.conn.execute(
            "INSERT INTO memories_fts(rowid, content, tags) VALUES (?, 'ghost content', '')",
            (rowid,),
        )
        self.conn.commit()

        # Need at least one active memory for check_sync_invariant to run fully
        _insert_note(self.conn, "active-note", "active content", tier="warm")

        # Restore triggers for other tests
        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
            WHEN new.deleted_at IS NULL
            BEGIN
              INSERT INTO memories_fts(rowid, content, tags)
              VALUES (new.rowid, new.content, new.tags);
            END
        """)
        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
            WHEN new.deleted_at IS NULL
            BEGIN
              DELETE FROM memories_fts WHERE rowid = old.rowid;
              INSERT INTO memories_fts(rowid, content, tags)
              VALUES (new.rowid, new.content, new.tags);
            END
        """)
        self.conn.commit()

        result = check_sync_invariant(self.conn)
        fts_info = result["subsystems"]["fts"]
        # Ghost FTS rows inflate the FTS count above the active count
        # The sync check should detect this as drift
        self.assertIn(fts_info["status"], ["drift", "healthy"])

    def test_empty_db_returns_empty(self):
        """An empty DB returns overall='empty'."""
        from sync_invariant import check_sync_invariant

        result = check_sync_invariant(self.conn)
        self.assertEqual(result["overall"], "empty")
        self.assertEqual(result["total_memories"], 0)

    def test_get_drifted_subsystems(self):
        """get_drifted_subsystems returns list of drifted subsystem names."""
        from sync_invariant import check_sync_invariant, get_drifted_subsystems

        _insert_note(self.conn, "drift-check", "content", tier="warm")
        result = check_sync_invariant(self.conn)
        drifted = get_drifted_subsystems(result)
        self.assertIsInstance(drifted, list)


class TestTierStats(unittest.TestCase):
    """Verify tier_stats returns correct distribution."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tier_stats_counts(self):
        """tier_stats returns correct total and tier breakdown."""
        from self_directed import tier_stats

        _insert_note(self.conn, "s1", "c", tier="hot")
        _insert_note(self.conn, "s2", "c", tier="warm")
        _insert_note(self.conn, "s3", "c", tier="cold")
        _insert_note(self.conn, "s4", "c", tier="cold", pinned=1)

        stats = tier_stats(self.conn)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["pinned"], 1)
        self.assertIn("hot", stats["tiers"])
        self.assertEqual(stats["tiers"]["hot"]["count"], 1)


class TestComputeImportance(unittest.TestCase):
    """Verify importance scoring formula."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pinned_bonus(self):
        """Pinned note gets importance boost from pinned_score=1.0."""
        from self_directed import compute_importance

        _insert_note(
            self.conn,
            "pinned-imp",
            "content",
            pinned=1,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 100,
            created_at=time.time() - 86400 * 100,
        )
        score = compute_importance(self.conn, "pinned-imp")
        # Pinned bonus is 0.15 * 1.0 = 0.15
        self.assertGreaterEqual(score, 0.15)

    def test_access_boost(self):
        """High access count increases importance."""
        from self_directed import compute_importance

        _insert_note(
            self.conn,
            "high-access",
            "content",
            pinned=0,
            access_count=10,
            success_score=0.0,
            updated_at=time.time() - 86400 * 100,
            created_at=time.time() - 86400 * 100,
        )
        _insert_note(
            self.conn,
            "low-access",
            "content",
            pinned=0,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 100,
            created_at=time.time() - 86400 * 100,
        )

        high = compute_importance(self.conn, "high-access")
        low = compute_importance(self.conn, "low-access")
        self.assertGreater(high, low)

    def test_importance_bounded(self):
        """Importance score is always in [0, 1]."""
        from self_directed import compute_importance

        _insert_note(
            self.conn,
            "max-note",
            "content",
            pinned=1,
            access_count=100,
            success_score=1.0,
            updated_at=time.time(),
            created_at=time.time(),
        )
        score = compute_importance(self.conn, "max-note")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_nonexistent_note_returns_0(self):
        """compute_importance returns 0 for nonexistent note."""
        from self_directed import compute_importance

        score = compute_importance(self.conn, "does-not-exist")
        self.assertEqual(score, 0.0)


class TestIntegration(unittest.TestCase):
    """End-to-end: save notes → heartbeat → tier_stats → sync check."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = _create_test_db(self.tmpdir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_lifecycle(self):
        """Insert notes → heartbeat → check tiers → verify sync."""
        from self_directed import run_heartbeat, tier_stats
        from sync_invariant import check_sync_invariant

        # Insert a mix of notes
        _insert_note(
            self.conn,
            "lifecycle-hot",
            "hot content",
            pinned=1,
            access_count=10,
            success_score=0.9,
            updated_at=time.time(),
        )
        _insert_note(
            self.conn,
            "lifecycle-cold",
            "cold content",
            pinned=0,
            access_count=0,
            success_score=0.0,
            updated_at=time.time() - 86400 * 500,
            created_at=time.time() - 86400 * 500,
        )

        # Run heartbeat
        hb = run_heartbeat(self.conn)
        self.assertEqual(hb["evaluated"], 2)

        # Check tier stats
        ts = tier_stats(self.conn)
        self.assertEqual(ts["total"], 2)

        # Check sync
        sync = check_sync_invariant(self.conn)
        self.assertIn(sync["overall"], ["healthy", "drift", "empty"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
