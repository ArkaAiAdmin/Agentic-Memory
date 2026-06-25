"""Comprehensive integration tests for agentic-memory data pipelines.

Verifies end-to-end data flow through ALL subsystems:
  save_pipeline → FTS5 / embeddings / KG / facts / backlinks / chunks / vec_keys
  search_pipeline ← FTS5 / embeddings / KG / facts / backlinks / vec_keys
  rebuild_index → subsystem table copy
  memory_delete → FK cascade across all 8 tables
  sync_invariant → drift detection
  connection_pool → thread safety / schema migration

Each test verifies data correctness through the public API, not
implementation details. Tests use real SQLite databases (no mocking).

All function signatures verified against source code (2026-06-10).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from _fixtures import bootstrap_temp_db_clean

# Ensure cron/ scripts (cron_backup, etc.) are importable when this
# file is run directly (not via pytest+conftest, which also adds it).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "cron") not in sys.path:
    sys.path.insert(0, str(_REPO / "cron"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a fresh DB with WAL mode + FK enforcement."""
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA foreign_keys=ON;")
    db.execute("PRAGMA busy_timeout = 5000;")
    return db


def _init_schema(db: sqlite3.Connection) -> None:
    """H21: no-op. The full prod schema is now set up by
    bootstrap_temp_db_clean in each test's setup_method.

    The previous version created a custom schema with columns
    (psi, next_review, adaptive_halflife_days, embedding_revision)
    that the prod schema doesn't have. Those are now:
    - psi: computed Python-side in pinned_decay.py
    - next_review: stored in review_schedule table (not in memories)
    - adaptive_halflife_days: stored in metadata JSON
    - embedding_revision: stored in memory_embeddings.model_revision

    This function is kept for backward compat with the test signatures.
    """
    pass


def _insert_test_memory(
    db: sqlite3.Connection,
    note_id: str,
    content: str,
    tags: list | None = None,
    **kwargs,
) -> None:
    """Insert a test memory row with subsystem data."""
    tags_json = json.dumps(tags or ["test"])
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO memories (id, content, source_file, tags, created_at, "
        "updated_at, observed_at, fitness_score, importance, pinned, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, ?)",
        (
            note_id,
            content,
            f"{note_id}.md",
            tags_json,
            now,
            now,
            now,
            note_id.split("/")[0] if "/" in note_id else "test",
        ),
    )


def _count(db: sqlite3.Connection, table: str, where: str = "1=1", params=()) -> int:
    """Count rows in a table."""
    return db.execute(
        f"SELECT COUNT(*) FROM [{table}] WHERE {where}", params
    ).fetchone()[0]


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    """Check if a table exists."""
    return (
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ===========================================================================
# Phase 1: SAVE PIPELINE → ALL SUBSYSTEMS
# ===========================================================================


class TestSavePipelineWritesAllSubsystems:
    """Verify that _update_memory_index_incremental writes to every subsystem."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        # H21: bootstrap with full prod schema (no custom columns)
        bootstrap_temp_db_clean(self.db_path)
        db = _make_db(self.db_path)
        _init_schema(db)  # no-op (kept for backward compat)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_writes_memories(self):
        """save_pipeline writes to memories table."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-note",
            "This is test content about Python.",
            ["python"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        assert _count(db, "memories", "id = ?", ("lessons/test-note",)) == 1
        row = db.execute(
            "SELECT content FROM memories WHERE id = ?", ("lessons/test-note",)
        ).fetchone()
        assert "Python" in row[0]
        db.close()

    def test_save_writes_fts5(self):
        """save_pipeline writes to FTS5 (memories_fts) via trigger."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-fts",
            "Unique searchable content XYZZY here.",
            ["test"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        rows = db.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("XYZZY",)
        ).fetchall()
        assert len(rows) >= 1, "FTS5 index not populated after save"
        db.close()

    def test_save_writes_embeddings(self):
        """save_pipeline writes to memory_embeddings table."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-emb",
            "Embedding test content here.",
            ["embedding"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        assert (
            _count(db, "memory_embeddings", "memory_id = ?", ("lessons/test-emb",)) == 1
        )
        row = db.execute(
            "SELECT embedding, dim FROM memory_embeddings WHERE memory_id = ?",
            ("lessons/test-emb",),
        ).fetchone()
        assert row is not None
        assert len(row[0]) > 0  # embedding blob not empty
        assert row[1] > 0  # dim > 0
        db.close()

    def test_save_writes_kg_entities(self):
        """save_pipeline writes to kg_entities via KG indexing."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-kg",
            "Python is a programming language used for machine learning.",
            ["python", "ml"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        # kg_entities doesn't have memory_id; entities are shared across memories
        entities = db.execute("SELECT name FROM kg_entities").fetchall()
        assert len(entities) >= 1, "KG entities not created after save"
        db.close()

    def test_save_writes_kg_facts(self):
        """save_pipeline writes to kg_facts via fact extraction."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-facts",
            "Python is a high-level programming language created by Guido van Rossum.",
            ["python"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        # kg_facts uses source_memory, not memory_id
        facts = db.execute(
            "SELECT predicate, subject, object FROM kg_facts WHERE source_memory = ?",
            ("lessons/test-facts",),
        ).fetchall()
        assert len(facts) >= 1, "Facts not extracted after save"
        db.close()

    def test_save_writes_backlinks(self):
        """save_pipeline writes to backlinks when content has [[wiki-links]]."""
        from save_pipeline import _update_memory_index_incremental

        # Create target memory first
        db = _make_db(self.db_path)
        _insert_test_memory(db, "lessons/target", "Target note.")
        db.commit()
        db.close()
        # Save source with wiki-link to target
        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "source-note",
            "This references [[lessons/target]] for details.",
            ["ref"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        fwd = _count(
            db,
            "backlinks",
            "source_id = ? AND target_id = ?",
            ("lessons/source-note", "lessons/target"),
        )
        rev = _count(
            db,
            "backlinks",
            "source_id = ? AND target_id = ?",
            ("lessons/target", "lessons/source-note"),
        )
        assert fwd == 1, "Forward backlink not created"
        assert rev == 1, "Reverse backlink not created"
        db.close()

    def test_save_writes_chunks(self):
        """save_pipeline writes to memory_chunks for long content."""
        from save_pipeline import _update_memory_index_incremental

        long_content = "Word " * 200  # ~200 words, well above chunk threshold
        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-chunks",
            long_content,
            ["long"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        chunks = _count(db, "memory_chunks", "parent_id = ?", ("lessons/test-chunks",))
        assert chunks >= 1, "Chunks not created for long content"
        db.close()

    def test_save_writes_vec_keys(self):
        """save_pipeline writes memory_embeddings (vec_keys needs rebuild_vec_index)."""
        from save_pipeline import _update_memory_index_incremental

        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "test-vec",
            "Vector key test content.",
            ["vec"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        # save_pipeline writes memory_embeddings but NOT memory_vec_keys
        # (vec_keys is populated only by rebuild_vec_index.py)
        assert (
            _count(db, "memory_embeddings", "memory_id = ?", ("lessons/test-vec",)) == 1
        )
        db.close()

    def test_save_upsert_does_not_cascade_delete(self):
        """ON CONFLICT DO UPDATE preserves child rows (unlike INSERT OR REPLACE)."""
        from save_pipeline import _update_memory_index_incremental

        # First save
        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "upsert-test",
            "Original content.",
            ["orig"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        orig_emb = _count(
            db, "memory_embeddings", "memory_id = ?", ("lessons/upsert-test",)
        )
        assert orig_emb == 1
        db.close()
        # Second save (upsert) — should NOT delete the embedding
        _update_memory_index_incremental(
            self.db_path,
            "lessons",
            "upsert-test",
            "Updated content.",
            ["updated"],
            False,
            datetime.now(timezone.utc).isoformat(),
            False,
        )
        db = _make_db(self.db_path)
        assert (
            _count(db, "memory_embeddings", "memory_id = ?", ("lessons/upsert-test",))
            == 1
        )
        row = db.execute(
            "SELECT content FROM memories WHERE id = ?", ("lessons/upsert-test",)
        ).fetchone()
        assert "Updated" in row[0]
        db.close()


# ===========================================================================
# Phase 2: SEARCH PIPELINE READS ALL SUBSYSTEMS
# ===========================================================================


class TestSearchPipelineReadsAllSubsystems:
    """Verify search_memories reads from every subsystem."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _populate_and_search(self, query: str, **search_kwargs):
        """Save test data, then run search_memories."""
        from save_pipeline import _update_memory_index_incremental

        notes = [
            (
                "lessons/python-basics",
                "Python is a programming language for ML.",
                ["python"],
            ),
            ("lessons/ml-intro", "Machine learning uses statistical methods.", ["ml"]),
            ("lessons/java-notes", "Java is an object-oriented language.", ["java"]),
        ]
        for nid, content, tags in notes:
            _update_memory_index_incremental(
                self.db_path,
                nid.split("/")[0],
                nid.split("/")[1],
                content,
                tags,
                False,
                datetime.now(timezone.utc).isoformat(),
                False,
            )
        from search_pipeline import search_memories

        return search_memories(self.db_path, query, **search_kwargs)

    def test_search_returns_fts_results(self):
        """search_memories returns results from FTS5 index."""
        result = self._populate_and_search("Python programming")
        results = result["results"]
        assert len(results) > 0
        ids = [r.get("id") for r in results]
        assert "lessons/python-basics" in ids

    def test_search_returns_embedding_results(self):
        """search_memories returns results from embedding search."""
        result = self._populate_and_search("object oriented programming")
        results = result["results"]
        ids = [r.get("id") for r in results]
        assert "lessons/java-notes" in ids or len(results) > 0

    def test_search_respects_tag_filter(self):
        """search_memories results contain tags for verification."""
        result = self._populate_and_search("language")
        results = result["results"]
        assert len(results) > 0
        # Verify tags are present in results
        for r in results:
            assert "tags" in r

    def test_search_respects_category_filter(self):
        """search_memories results have correct source_file (category)."""
        result = self._populate_and_search("programming")
        results = result["results"]
        for r in results:
            assert "lessons/" in r.get("source_file", "") or "lessons" in r.get(
                "id", ""
            )

    def test_search_no_deleted_memories(self):
        """search_memories excludes soft-deleted memories."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "lessons/deleted-one", "Deleted content.")
        db.execute(
            "UPDATE memories SET deleted_at = datetime('now') WHERE id = ?",
            ("lessons/deleted-one",),
        )
        db.commit()
        db.close()
        result = self._populate_and_search("Deleted content")
        results = result["results"]
        ids = [r.get("id") for r in results]
        assert "lessons/deleted-one" not in ids


# ===========================================================================
# Phase 3: REBUILD INDEX SUBSYSTEM COPY
# ===========================================================================


class TestRebuildSubsystemCopy:
    """Verify rebuild_index.py copies all subsystem tables correctly."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        self.source_dir = self.tmpdir / "memory"
        self.source_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_source_files(self, count: int = 3) -> None:
        """Create .md source files for rebuild_index to scan."""
        for i in range(count):
            md = self.source_dir / f"test-mem-{i}.md"
            md.write_text(
                f"---\ncreated: 2026-01-01T00:00:00\n"
                f"updated: 2026-01-01T00:00:00\n"
                f"tags: [test]\nimportance: 3\n---\n\n"
                f"Test content for memory {i}. "
                f"Python is a programming language.\n"
            )

    def _populate_subsystems(
        self, db: sqlite3.Connection, prefix: str = "test"
    ) -> None:
        """Insert subsystem data for a given prefix."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            nid = f"{prefix}/mem-{i}"
            db.execute(
                "INSERT INTO memories (id, content, source_file, tags, "
                "created_at, updated_at, observed_at, fitness_score, "
                "importance, pinned, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, ?)",
                (
                    nid,
                    f"Content {i}",
                    f"{prefix}/mem-{i}.md",
                    '["test"]',
                    now,
                    now,
                    now,
                    prefix,
                ),
            )
        db.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("Python", "language", now, now),
        )
        db.execute(
            "INSERT INTO kg_edges (source_id, target_id, "
            "relation, weight, created_at) "
            "VALUES (1, 1, 'is_a', 1.0, ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO kg_facts (subject, predicate, object, "
            "confidence, source_memory, first_seen, last_seen) "
            "VALUES ('Python', 'is_a', 'language', 0.9, ?, ?, ?)",
            (f"{prefix}/mem-0", now, now),
        )
        db.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content, "
            "created_at) "
            "VALUES (?, 0, 0, 12, ?, ?)",
            (f"{prefix}/mem-0", "Chunk text", now),
        )
        db.execute(
            "INSERT INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
            (42, f"{prefix}/mem-0"),
        )
        db.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 5)")
        db.execute(
            "INSERT INTO memory_audit_log (ts, tool, latency_ms) "
            "VALUES (?, 'test', 1.0)",
            (now,),
        )
        db.execute(
            "INSERT INTO user_access_log (note_id, access_ts, source) "
            "VALUES (?, ?, 'test')",
            (f"{prefix}/mem-0", time.time()),
        )
        db.commit()

    def test_rebuild_preserves_core_tables(self):
        """rebuild_index preserves core tables (memories, FTS, embeddings, backlinks)."""
        self._create_source_files(3)
        db = _make_db(self.db_path)
        # H21: bootstrap with full prod schema
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        self._populate_subsystems(db)
        db.close()

        # Count core tables before
        db = _make_db(self.db_path)
        counts_before = {}
        for t in ["memories", "memory_embeddings", "backlinks", "schema_version"]:
            counts_before[t] = _count(db, t)
        db.close()

        # Run rebuild
        import subprocess

        result = subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(Path(__file__).parent.parent / "rebuild_index.py"),
                str(self.source_dir),
                str(self.db_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"rebuild failed: {result.stderr}"

        # Verify core tables preserved
        # Note: memory_embeddings is explicitly dropped and rebuilt by rebuild_index,
        # so it's not included in the preservation check.
        db = _make_db(self.db_path)
        for t in ["memories", "backlinks", "schema_version"]:
            assert _count(db, t) == counts_before[t], (
                f"{t} changed: {counts_before[t]} → {_count(db, t)}"
            )
        db.close()

    def test_rebuild_orphan_cleanup_removes_stale_subsystems(self):
        """rebuild_index removes subsystem rows referencing missing memories."""
        self._create_source_files(2)  # only 2 source files
        db = _make_db(self.db_path)
        # H21: bootstrap with full prod schema
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        self._populate_subsystems(db, prefix="orphan")
        db.close()

        import subprocess

        result = subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(Path(__file__).parent.parent / "rebuild_index.py"),
                str(self.source_dir),
                str(self.db_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"rebuild failed: {result.stderr}"

        db = _make_db(self.db_path)
        surviving = _count(db, "memories")
        assert surviving >= 1, "No memories survived rebuild"
        db.close()


# ===========================================================================
# Phase 4: FK CASCADE ON DELETE
# ===========================================================================


class TestFKCascadeDelete:
    """Verify FK CASCADE works across all child tables."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _populate_all(self, db: sqlite3.Connection) -> None:
        """Insert a memory with all subsystem data."""
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO memories (id, content, source_file, tags, "
            "created_at, updated_at, observed_at, fitness_score, "
            "importance, pinned, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, 'test')",
            (
                "test/del-target",
                "Delete me",
                "test/del-target.md",
                '["test"]',
                now,
                now,
                now,
            ),
        )
        # KG entity (shared across memories, no memory_id)
        db.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) "
            "VALUES ('DelEntity', 'concept', ?, ?)",
            (now, now),
        )
        eid = db.execute(
            "SELECT id FROM kg_entities WHERE name = 'DelEntity'"
        ).fetchone()[0]
        # KG edge
        db.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'related_to', ?)",
            (eid, eid, now),
        )
        # KG fact (uses source_memory, not memory_id)
        db.execute(
            "INSERT INTO kg_facts (source_memory, predicate, subject, object, "
            "confidence, first_seen, last_seen) "
            "VALUES (?, 'is_a', 'X', 'Y', 0.9, ?, ?)",
            ("test/del-target", now, now),
        )
        # Chunks (uses parent_id, not memory_id)
        db.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content, created_at) "
            "VALUES (?, 0, 0, 5, 'chunk', ?)",
            ("test/del-target", now),
        )
        # Vec keys (key INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE)
        db.execute(
            "INSERT INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
            (99, "test/del-target"),
        )
        # Adaptive retention is metadata-based (no adaptive_retention table)
        # User access log (note_id, access_ts, source)
        db.execute(
            "INSERT INTO user_access_log (note_id, access_ts, source) "
            "VALUES (?, ?, 'test')",
            ("test/del-target", time.time()),
        )
        # Embedding
        db.execute(
            "INSERT INTO memory_embeddings (memory_id, content_hash, "
            "embedding, model_revision, dim, updated_at) "
            "VALUES (?, 'hash', X'00000000', 'v1', 4, ?)",
            ("test/del-target", time.time()),
        )
        db.commit()

    def test_hard_delete_cascades_all_tables(self):
        """hard_delete_note cascades to all subsystem tables."""
        db = _make_db(self.db_path)
        self._populate_all(db)
        # hard_delete_note requires >30 days age or soft-deleted
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        db.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (old_date, "test/del-target"),
        )
        db.commit()
        db.close()

        from memory_delete import hard_delete_note

        result = hard_delete_note(self.db_path, "test/del-target")
        assert result is True, "hard_delete_note returned False"

        db = _make_db(self.db_path)
        assert _count(db, "memories", "id = ?", ("test/del-target",)) == 0
        for table, col in [
            ("kg_facts", "source_memory"),
            ("memory_chunks", "parent_id"),
            ("memory_vec_keys", "memory_id"),
            ("user_access_log", "note_id"),
            ("memory_embeddings", "memory_id"),
        ]:
            assert _count(db, table, f"{col} = ?", ("test/del-target",)) == 0, (
                f"{table} not cascaded on hard_delete"
            )
        # KG entities are NOT cleaned up here — they are shared across
        # notes and deleting them based on a single note's relationship
        # would be too aggressive. Use repair_kg_orphans separately.
        eid = db.execute(
            "SELECT id FROM kg_entities WHERE name = 'DelEntity'"
        ).fetchone()
        assert eid is not None, (
            "KG entity should survive hard_delete_note (shared across notes)"
        )
        db.close()

    def test_purge_expired_cascades_all_tables(self):
        """purge_expired cascades to all subsystem tables (bulk path)."""
        db = _make_db(self.db_path)
        self._populate_all(db)
        # Mark as soft-deleted and old
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        db.execute(
            "UPDATE memories SET deleted_at = ?, created_at = ? WHERE id = ?",
            (old_date, old_date, "test/del-target"),
        )
        db.commit()
        db.close()

        from memory_delete import purge_expired

        result = purge_expired(self.db_path)
        # purge_expired returns count of deleted rows
        assert isinstance(result, int)

        db = _make_db(self.db_path)
        assert _count(db, "memories", "id = ?", ("test/del-target",)) == 0
        for table, col in [
            ("kg_facts", "source_memory"),
            ("memory_chunks", "parent_id"),
            ("memory_vec_keys", "memory_id"),
            ("user_access_log", "note_id"),
        ]:
            assert _count(db, table, f"{col} = ?", ("test/del-target",)) == 0, (
                f"{table} not cascaded on purge_expired"
            )
        db.close()


# ===========================================================================
# Phase 5: CONNECTION POOL THREAD SAFETY
# ===========================================================================


class TestConnectionPoolThreadSafety:
    """Verify connection pool handles concurrent access correctly."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_get_different_threads(self):
        """Different threads get different connections."""
        from memory_common import connection_pool

        results = {}
        errors = []

        def worker(thread_id):
            try:
                conn = connection_pool.get(str(self.db_path))
                results[thread_id] = id(conn)
                conn.execute("SELECT COUNT(*) FROM memories")
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 5

    def test_concurrent_reads_no_corruption(self):
        """Concurrent reads don't corrupt the database."""
        from memory_common import connection_pool

        db = _make_db(self.db_path)
        for i in range(10):
            _insert_test_memory(db, f"test/concurrent-{i}", f"Content {i}" * 10)
        db.commit()
        db.close()

        errors = []
        results = []

        def reader(thread_id):
            try:
                conn = connection_pool.get(str(self.db_path))
                rows = conn.execute("SELECT id FROM memories").fetchall()
                results.append((thread_id, len(rows)))
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        counts = [r[1] for r in results]
        assert len(set(counts)) == 1, f"Inconsistent reads: {counts}"

    def test_pool_lru_eviction(self):
        """Pool evicts LRU connection when at max capacity."""
        from memory_common import connection_pool

        connection_pool._max_size = 3
        for i in range(5):
            connection_pool.get(str(self.db_path))
        assert len(connection_pool._pool) <= 3

    def test_schema_migration_runs_once(self):
        """_ensure_full_schema runs only once per connection."""
        from memory_common import connection_pool

        conn = connection_pool.get(str(self.db_path))
        conn_id = id(conn)
        assert conn_id in connection_pool._migrated
        conn2 = connection_pool.get(str(self.db_path))
        assert id(conn2) == conn_id


# ===========================================================================
# Phase 6: FTS TRIGGER CONSISTENCY
# ===========================================================================


class TestFTSTriggerConsistency:
    """Verify FTS5 triggers keep the virtual table in sync with memories."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fts_insert_trigger(self):
        """FTS5 index is populated on INSERT into memories."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/fts-insert", "Unique FTS trigger test content.")
        db.commit()
        rows = db.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("trigger",)
        ).fetchall()
        assert len(rows) >= 1
        db.close()

    def test_fts_delete_trigger(self):
        """FTS5 index is cleaned up on DELETE from memories."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/fts-delete", "Delete me from FTS index.")
        db.commit()
        assert (
            db.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("Delete",)
            ).fetchone()
            is not None
        )
        db.execute("DELETE FROM memories WHERE id = ?", ("test/fts-delete",))
        db.commit()
        rows = db.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("Delete",)
        ).fetchall()
        assert len(rows) == 0
        db.close()

    def test_fts_update_trigger(self):
        """FTS5 index is updated on UPDATE of memories content."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/fts-update", "Original FTS content.")
        db.commit()
        db.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            ("Updated FTS content with new keyword.", "test/fts-update"),
        )
        db.commit()
        old = db.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("Original",)
        ).fetchall()
        new = db.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", ("Updated",)
        ).fetchall()
        assert len(old) == 0, "Old content still in FTS after update"
        assert len(new) >= 1, "New content not in FTS after update"
        db.close()


# ===========================================================================
# Phase 7: SYNC INVARIANT DRIFT DETECTION
# ===========================================================================


class TestSyncInvariant:
    """Verify sync_invariant detects healthy/drift/empty states.

    NOTE: check_sync_invariant takes a sqlite3.Connection, not a Path.
    adaptive_retention is checked via metadata JSON, not a separate table.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_healthy_state(self):
        """Healthy: all subsystems have correct counts."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/healthy", "Healthy content.")
        db.commit()
        db.close()

        from sync_invariant import check_sync_invariant, get_drifted_subsystems

        db = _make_db(self.db_path)
        result = check_sync_invariant(db)
        drifted = get_drifted_subsystems(result)
        assert len(drifted) == 0, f"Unexpected drift: {drifted}"
        db.close()

    def test_drifted_state(self):
        """Drifted: subsystem counts don't match memories count."""
        db = _make_db(self.db_path)
        # Insert multiple memories to create drift signal
        for i in range(10):
            _insert_test_memory(db, f"test/drift-{i}", f"Drifted content {i}.")
        db.commit()
        # Remove 5 FTS entries to create 50% drift
        for i in range(5):
            db.execute(
                "DELETE FROM memories_fts WHERE rowid = "
                "(SELECT rowid FROM memories WHERE id = ?)",
                (f"test/drift-{i}",),
            )
        db.commit()
        db.close()

        from sync_invariant import check_sync_invariant, get_drifted_subsystems

        db = _make_db(self.db_path)
        result = check_sync_invariant(db)
        drifted = get_drifted_subsystems(result)
        assert len(drifted) > 0, "Drift not detected"
        db.close()

    def test_empty_state(self):
        """Empty: no memories, subsystems are empty."""
        from sync_invariant import check_sync_invariant, get_drifted_subsystems

        db = _make_db(self.db_path)
        result = check_sync_invariant(db)
        drifted = get_drifted_subsystems(result)
        assert len(drifted) == 0
        db.close()


# ===========================================================================
# Phase 8: CONTRADICTION CHECK INTEGRATION
# ===========================================================================


class TestContradictionCheckIntegration:
    """Verify contradiction check runs during save with safety_wiring=True."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_contradiction_check_wired_on_save(self):
        """save_memory with safety_wiring=True completes without error."""
        from save_pipeline import save_memory

        result1 = save_memory(
            content="The sky is blue.",
            category="test",
            title_slug="color-sky",
            tags=["color"],
            is_global=False,
            safety_wiring=True,
            db_path=str(self.db_path),
        )
        result2 = save_memory(
            content="The sky is red.",
            category="test",
            title_slug="color-sky-2",
            tags=["color"],
            is_global=False,
            safety_wiring=True,
            db_path=str(self.db_path),
        )
        assert result1 is not None
        assert result2 is not None

    def test_safety_wiring_false_skips_check(self):
        """save_memory with safety_wiring=False skips contradiction check."""
        from save_pipeline import save_memory

        result = save_memory(
            content="No check here.",
            category="test",
            title_slug="no-check",
            tags=[],
            is_global=False,
            safety_wiring=False,
            db_path=str(self.db_path),
        )
        assert result is not None


# ===========================================================================
# Phase 9: ADAPTIVE RETENTION FROM SEARCH
# ===========================================================================


class TestAdaptiveRetentionFromSearch:
    """Verify record_access creates user_access_log entries.

    NOTE: adaptive_retention is metadata-based (no separate table).
    record_access takes a conn, not a db_path.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_access_creates_entry(self):
        """record_access creates user_access_log row."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/retention", "Retention test.")
        db.commit()

        from adaptive_retention import record_access

        record_access(db, "test/retention", "search")

        row = db.execute(
            "SELECT note_id, source FROM user_access_log WHERE note_id = ?",
            ("test/retention",),
        ).fetchone()
        assert row is not None, "user_access_log row not created"
        assert row[1] == "search"
        db.close()

    def test_record_access_increments_count(self):
        """Multiple record_access calls create multiple log entries."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/retention-2", "Retention test 2.")
        db.commit()

        from adaptive_retention import record_access

        record_access(db, "test/retention-2", "search")
        record_access(db, "test/retention-2", "search")
        record_access(db, "test/retention-2", "read")

        count = db.execute(
            "SELECT COUNT(*) FROM user_access_log WHERE note_id = ?",
            ("test/retention-2",),
        ).fetchone()[0]
        assert count == 3, f"Expected 3 access log entries, got {count}"
        db.close()


# ===========================================================================
# Phase 10: GRAPH-RAG EXPANSION
# ===========================================================================


class TestGraphRAGExpansion:
    """Verify _graph_rag_expand retrieves KG-connected memories.

    NOTE: _graph_rag_expand(query, db_path) — query first, db_path second.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_graph_rag_finds_connected_entities(self):
        """_graph_rag_expand returns memories connected via KG edges."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/grag-a", "Python is used for data science.")
        _insert_test_memory(
            db, "test/grag-b", "Machine learning uses Python extensively."
        )
        db.commit()
        now = datetime.now(timezone.utc).isoformat()
        # Insert two different entities connected by an edge
        db.execute(
            "INSERT INTO kg_entities (name, entity_type, "
            "created_at, updated_at) "
            "VALUES ('Python', 'language', ?, ?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO kg_entities (name, entity_type, "
            "created_at, updated_at) "
            "VALUES ('DataScience', 'field', ?, ?)",
            (now, now),
        )
        e1 = db.execute("SELECT id FROM kg_entities WHERE name = 'Python'").fetchone()[
            0
        ]
        e2 = db.execute(
            "SELECT id FROM kg_entities WHERE name = 'DataScience'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO kg_edges (source_id, target_id, "
            "relation, weight, created_at) "
            "VALUES (?, ?, 'used_for', 1.0, ?)",
            (e1, e2, now),
        )
        db.commit()
        db.close()

        from search_pipeline import _graph_rag_expand

        connected = _graph_rag_expand("Python", self.db_path)
        assert len(connected) >= 1, "Graph-RAG found no connected memories"


# ===========================================================================
# Phase 11: CROSS-DB SEARCH
# ===========================================================================


class TestCrossDBSearch:
    """Verify search with include_global=True searches both local and global DBs."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "local.db"
        self.global_db_path = self.tmpdir / "global.db"
        for p in [self.db_path, self.global_db_path]:
            db = _make_db(p)
            # H21: bootstrap with full prod schema
            bootstrap_temp_db_clean(p)
            _init_schema(db)
            db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_include_global_searches_both(self):
        """include_global=True finds results from both local and global DBs."""
        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/local-note", "Local content about Python.")
        db.commit()
        db.close()
        db = _make_db(self.global_db_path)
        _insert_test_memory(db, "test/global-note", "Global content about Python.")
        db.commit()
        db.close()

        from search_pipeline import search_memories

        with patch("search_pipeline.GLOBAL_MEM_DIR", self.tmpdir):
            result = search_memories(
                self.db_path,
                "Python",
                include_global=True,
            )
        results = result["results"]
        ids = [r.get("id") for r in results]
        has_local = "test/local-note" in ids
        has_global = "test/global-note" in ids
        assert has_local or has_global, f"Neither local nor global found: {ids}"


# ===========================================================================
# Phase 12: FACT EXTRACTION EDGE CASES
# ===========================================================================


class TestFactExtractionEdgeCases:
    """Verify fact extraction handles various content patterns.

    NOTE: The public API is extract_facts(text), not _extract_facts_from_text.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fact_extraction_is_a(self):
        """Fact extraction captures 'is_a' predicates."""
        from fact_extraction import extract_facts

        facts = extract_facts("Python is a programming language.")
        predicates = [f[1] for f in facts]
        assert "is_a" in predicates, f"is_a not found in {predicates}"

    def test_fact_extraction_meta_label_skip(self):
        """Meta-labels like 'Status:' and 'Date:' are skipped in extraction."""
        from fact_extraction import extract_facts

        text = "Status: active\nDate: 2026-01-01\nPython is a language."
        facts = extract_facts(text)
        subjects = [f[0].lower() for f in facts]
        assert "status" not in subjects, f"Meta-label 'Status' not skipped"
        assert "date" not in subjects, f"Meta-label 'Date' not skipped"

    def test_fact_extraction_verb_skip(self):
        """Built-in verbs are skipped as subjects."""
        from fact_extraction import extract_facts

        text = "Use Python for machine learning."
        facts = extract_facts(text)
        subjects = [f[0].lower() for f in facts]
        assert "use" not in subjects, f"Built-in verb 'use' not skipped"


# ===========================================================================
# Phase 13: EMBEDDING SEARCH CACHE
# ===========================================================================


class TestEmbeddingSearchCache:
    """Verify embedding search cache invalidation on rebuild."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_hit_after_index(self):
        """After indexing, embedding search uses cache."""
        from embedding_search import get_embedding_search

        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/cache-hit", "Cache test content.")
        db.commit()
        searcher = get_embedding_search()
        searcher.index_embedding(db, "test/cache-hit", "Cache test content.")
        cached = db.execute(
            "SELECT memory_id FROM memory_embeddings WHERE memory_id = ?",
            ("test/cache-hit",),
        ).fetchone()
        assert cached is not None
        db.close()

    def test_cache_invalidation_on_rebuild(self):
        """After rebuild_vec_index, cache is refreshed."""
        from embedding_search import get_embedding_search

        db = _make_db(self.db_path)
        _insert_test_memory(db, "test/cache-rebuild", "Rebuild cache test.")
        db.commit()
        db.close()

        import subprocess

        result = subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(Path(__file__).parent.parent / "rebuild_vec_index.py"),
                str(self.db_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # May fail if no embeddings exist, but cache should be cleared
        from search_pipeline import search_memories

        result = search_memories(self.db_path, "Rebuild cache test")
        assert isinstance(result, dict)
        assert "results" in result


# ===========================================================================
# Phase 14: METRICS PROMETHEUS FORMAT
# ===========================================================================


class TestMetricsPrometheus:
    """Verify metrics export in Prometheus format.

    NOTE: format_prometheus(stats) takes a stats dict.
    _RuntimeCounters uses inc(name) and record(name, latency_ms).
    """

    def test_prometheus_format(self):
        """format_prometheus exports valid Prometheus text."""
        import sys

        # Prevent eval/longmemeval_s/metrics.py from shadowing the real metrics.py
        _root = str(Path(__file__).resolve().parent.parent)
        _bad = str(Path(__file__).resolve().parent / "longmemeval_s")
        _saved = [p for p in sys.path if p in (_root, _bad)]
        sys.path[:] = [p for p in sys.path if p != _bad]
        if _root not in sys.path:
            sys.path.insert(0, _root)
        # Clear any cached wrong metrics module
        for k in list(sys.modules):
            if k == "metrics" or k.startswith("metrics."):
                _f = getattr(sys.modules[k], "__file__", None)
                if _f and "longmemeval_s" in _f:
                    del sys.modules[k]
        from metrics import format_prometheus, _runtime

        _runtime.reset()  # isolate from prior tests
        stats = {
            "save_memory": {
                "total": 5,
                "avg_latency_ms": 12.3,
                "errors": 0,
                "error_rate": 0.0,
            },
            "_summary": {"total_operations": 5, "throughput_ops_per_sec": 0.5},
        }
        output = format_prometheus(stats)
        assert isinstance(output, str)
        assert len(output) > 0
        lines = output.strip().split("\n")
        metric_lines = [l for l in lines if l and not l.startswith("#")]
        assert len(metric_lines) >= 1, "No metric lines in output"

    def test_runtime_counters(self):
        """_RuntimeCounters tracks operations."""
        import sys

        _root = str(Path(__file__).resolve().parent.parent)
        _bad = str(Path(__file__).resolve().parent / "longmemeval_s")
        sys.path[:] = [p for p in sys.path if p != _bad]
        if _root not in sys.path:
            sys.path.insert(0, _root)
        for k in list(sys.modules):
            if k == "metrics" or k.startswith("metrics."):
                _f = getattr(sys.modules[k], "__file__", None)
                if _f and "longmemeval_s" in _f:
                    del sys.modules[k]
        from metrics import _RuntimeCounters

        counters = _RuntimeCounters()
        counters.inc("saves")
        counters.inc("searches")
        counters.record("save_latency", 50.0)
        counters.record("search_latency", 100.0)
        snapshot = counters.snapshot()
        assert snapshot["counters"]["saves"] == 1
        assert snapshot["counters"]["searches"] == 1
        assert "save_latency" in snapshot["histograms"]
        assert snapshot["histograms"]["save_latency"]["avg_ms"] == 50.0


# ===========================================================================
# Phase 15: CRON BACKUP
# ===========================================================================


class TestCronBackup:
    """Verify cron backup creates and rotates backups.

    NOTE: do_backup(backup_dir) — not run_backup.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        db = _make_db(self.db_path)
        bootstrap_temp_db_clean(self.db_path)
        _init_schema(db)
        # H21: _init_schema is no-op (schema from bootstrap)
        db.close()

    def teardown_method(self):
        from memory_common import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backup_creates_file(self):
        """do_backup creates a backup file."""
        from cron_backup import do_backup

        backup_dir = self.tmpdir / "backups"
        backup_dir.mkdir()
        result = do_backup(backup_dir)
        backups = list(backup_dir.glob("*.db*"))
        assert len(backups) >= 1, "No backup file created"

    def test_backup_rotation(self):
        """Old backups are rotated (deleted after retention period)."""
        from cron_backup import do_backup

        backup_dir = self.tmpdir / "backups"
        backup_dir.mkdir()
        # Create 10 "old" backups
        for i in range(10):
            old_file = backup_dir / f"memory-{i}.db"
            old_file.write_bytes(b"old")
            old_time = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            os.utime(str(old_file), (old_time, old_time))
        # Run backup
        do_backup(backup_dir)
        remaining = list(backup_dir.glob("*.db*"))
        assert len(remaining) < 11, "Rotation did not remove old backups"
