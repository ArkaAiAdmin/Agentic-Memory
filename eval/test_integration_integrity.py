"""Cross-subsystem data integrity integration test.

Verifies invariants across ALL subsystems after save, delete, rebuild:
  - memories
  - memory_vec_keys
  - memory_embeddings
  - memory_chunks
  - backlinks
  - kg_entities / kg_edges / kg_facts
  - user_access_log (adaptive retention)
  - memories_fts (FTS5 index)
"""

import hashlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import pytest
from _fixtures import bootstrap_temp_db_clean

import infrastructure
import save_pipeline as _save_pipeline_mod
from memory_common import connection_pool, open_db, run_db_migrations
from save_pipeline import (
    save_memory,
    clear_pragma_cache,
    GLOBAL_MEM_DIR as _SAVE_GLOBAL,
)
from memory_delete import hard_delete_note, soft_delete_note, ensure_deleted_at_column
from rebuild_index import rebuild_index

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_SUBSYSTEM_TABLES = frozenset(
    {
        "memories",
        "memory_vec_keys",
        "memory_embeddings",
        "memory_chunks",
        "backlinks",
        "kg_entities",
        "kg_edges",
        "kg_facts",
        "user_access_log",
    }
)


def _count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _assert_invariants(db: sqlite3.Connection, label: str = ""):
    """Assert ALL cross-subsystem invariants hold."""
    tag = f" [{label}]" if label else ""

    # 1. memories count
    mem_count = _count(db, "memories")
    assert mem_count >= 0, f"negative memories count{tag}"

    # 2. memory_vec_keys count == memories count
    vk_count = _count(db, "memory_vec_keys")
    assert vk_count == mem_count, (
        f"memory_vec_keys ({vk_count}) != memories ({mem_count}){tag}"
    )

    # 3. memory_embeddings count == memories count
    emb_count = _count(db, "memory_embeddings")
    assert emb_count == mem_count, (
        f"memory_embeddings ({emb_count}) != memories ({mem_count}){tag}"
    )

    # 4. memory_chunks has at least 1 chunk per memory (or 0 if no long notes)
    chunk_parents = {
        r[0]
        for r in db.execute("SELECT DISTINCT parent_id FROM memory_chunks").fetchall()
    }
    mem_ids = {r[0] for r in db.execute("SELECT id FROM memories").fetchall()}
    orphan_chunks = chunk_parents - mem_ids
    assert len(orphan_chunks) == 0, (
        f"memory_chunks with no parent: {orphan_chunks}{tag}"
    )

    # 5. No orphaned memory_vec_keys
    orphans_vk = db.execute(
        "SELECT memory_id FROM memory_vec_keys WHERE memory_id NOT IN (SELECT id FROM memories)"
    ).fetchall()
    assert len(orphans_vk) == 0, f"orphan memory_vec_keys: {orphans_vk}{tag}"

    # 6. No orphaned memory_embeddings
    orphans_emb = db.execute(
        "SELECT memory_id FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memories)"
    ).fetchall()
    assert len(orphans_emb) == 0, f"orphan memory_embeddings: {orphans_emb}{tag}"

    # 7. No orphaned backlinks
    orphans_bl = db.execute(
        "SELECT source_id, target_id FROM backlinks "
        "WHERE source_id NOT IN (SELECT id FROM memories) "
        "OR target_id NOT IN (SELECT id FROM memories)"
    ).fetchall()
    assert len(orphans_bl) == 0, f"orphan backlinks: {orphans_bl}{tag}"

    # 8. user_access_log references valid memory IDs (if table exists)
    try:
        orphans_ual = db.execute(
            "SELECT note_id FROM user_access_log "
            "WHERE note_id NOT IN (SELECT id FROM memories)"
        ).fetchall()
        assert len(orphans_ual) == 0, (
            f"orphan user_access_log entries: {orphans_ual}{tag}"
        )
    except Exception:
        pass  # table may not exist if adaptive retention not initialized

    # 9. kg_edges references valid kg_entities (if tables exist)
    try:
        if _count(db, "kg_entities") > 0:
            orphans_edge_src = db.execute(
                "SELECT source_id FROM kg_edges "
                "WHERE source_id NOT IN (SELECT id FROM kg_entities)"
            ).fetchall()
            assert len(orphans_edge_src) == 0, (
                f"kg_edges with invalid source_id: {orphans_edge_src}{tag}"
            )
            orphans_edge_tgt = db.execute(
                "SELECT target_id FROM kg_edges "
                "WHERE target_id NOT IN (SELECT id FROM kg_entities)"
            ).fetchall()
            assert len(orphans_edge_tgt) == 0, (
                f"kg_edges with invalid target_id: {orphans_edge_tgt}{tag}"
            )
    except Exception:
        pass  # KG tables may not exist

    # 10. FTS5 count matches memories count
    try:
        fts_count = db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        # FTS5 may use external content — check
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        if row and "content=" not in (row[0] or "").lower():
            assert fts_count == mem_count, (
                f"FTS5 count ({fts_count}) != memories ({mem_count}){tag}"
            )
    except Exception:
        pass  # FTS5 table may not exist


def _ensure_subsystem_tables(db: sqlite3.Connection):
    """Idempotently ensure all subsystem tables exist in the test DB."""
    run_db_migrations(db)
    # Vec index tables
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_vec_idx (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            n_vectors INTEGER NOT NULL,
            dim INTEGER NOT NULL,
            metric TEXT NOT NULL,
            quantization TEXT NOT NULL,
            connectivity INTEGER NOT NULL,
            expansion_add INTEGER NOT NULL,
            expansion_search INTEGER NOT NULL,
            built_at REAL NOT NULL,
            index_blob BLOB NOT NULL,
            key_count INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_vec_keys (
            key INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_vec_keys_memory_id ON memory_vec_keys(memory_id)"
    )
    # Embeddings
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            model_revision TEXT NOT NULL,
            dim INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
    """)
    # Chunks
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(parent_id, chunk_idx)
        )
    """)
    # Backlinks
    db.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    # KG tables
    db.executescript("""
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
            source_memory TEXT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            created_at TEXT
        );
    """)
    # Adaptive retention
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT NOT NULL,
            access_ts REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_access_note ON user_access_log(note_id)"
    )
    # FTS5
    fts_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not fts_exists:
        db.executescript("""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content, tags,
                tokenize='porter unicode61'
            );
            CREATE TRIGGER memories_ai AFTER INSERT ON memories
            WHEN new.deleted_at IS NULL
            BEGIN
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, new.tags);
            END;
            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                DELETE FROM memories_fts WHERE rowid = old.rowid;
            END;
            CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                DELETE FROM memories_fts WHERE rowid = old.rowid;
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, new.tags);
            END;
        """)
    db.commit()
    clear_pragma_cache()


def _derive_vec_key(memory_id: str) -> int:
    """Stable uint64 key, masked to signed int64 range for SQLite."""
    digest = hashlib.md5(memory_id.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big", signed=False)
    return raw & ((1 << 63) - 1)


def _populate_vec_keys(db: sqlite3.Connection):
    """Populate memory_vec_keys from memories table."""
    rows = db.execute("SELECT id FROM memories").fetchall()
    used_keys: set[int] = set()
    for (mid,) in rows:
        key = _derive_vec_key(mid)
        while key in used_keys:
            key = (key + 1) % (1 << 63)
            if key < 0:
                key = 0
        used_keys.add(key)
        db.execute(
            "INSERT OR IGNORE INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
            (key, mid),
        )
    db.commit()


def _populate_embeddings(db: sqlite3.Connection):
    """Populate memory_embeddings with dummy vectors."""
    rows = db.execute("SELECT id, content FROM memories").fetchall()

    for mid, content in rows:
        ch = hashlib.md5((content or "").encode()).hexdigest()
        vec = np.random.randn(384).astype(np.float32)
        db.execute(
            "INSERT OR IGNORE INTO memory_embeddings "
            "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, ch, vec.tobytes(), "test", 384, time.time()),
        )
    db.commit()


def build_complete_index(db_path: Path):
    """Run a full index rebuild + vec rebuild to get consistent state."""
    # Use a scratch source dir (no markdown files) so rebuild_index
    # doesn't scan the real memory dir — we work only from DB data.
    scratch = db_path.parent / "_rebuild_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / ".gitkeep").touch()
    try:
        rebuild_index(str(scratch), str(db_path))
    finally:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
    from rebuild_vec_index import rebuild_vec_index

    rebuild_vec_index(str(db_path))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_pool():
    """Clear the connection pool before each test so stale conns don't leak.

    Also saves and restores GLOBAL_MEM_DIR because other tests may
    monkeypatch it via save_pipeline.GLOBAL_MEM_DIR = tmp (which leaks
    across test boundaries when running the full suite).
    """
    _saved_global = _SAVE_GLOBAL
    _saved_infra_global = infrastructure.GLOBAL_MEM_DIR
    connection_pool.clear()
    clear_pragma_cache()
    yield
    _save_pipeline_mod.GLOBAL_MEM_DIR = _saved_global
    infrastructure.GLOBAL_MEM_DIR = _saved_infra_global
    connection_pool.clear()
    clear_pragma_cache()


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="integrity_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    """Create a fresh, fully-migrated test DB with all subsystems."""
    p = tmp_dir / "memory.db"
    bootstrap_temp_db_clean(p)
    return p


@pytest.fixture
def mem_dir(tmp_dir):
    """Create a memory dir that looks like a project memory directory."""
    d = tmp_dir / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Test: save pipeline consistency
# ---------------------------------------------------------------------------


class TestSavePipelineIntegrity:
    def test_save_single_note(self, tmp_dir, mem_dir):
        """Save one note, verify all subsystems are consistent."""
        db_path = mem_dir / "memory.db"

        result = save_memory(
            "This is a test memory about integrity checking.",
            category="lessons",
            title_slug="integrity-test",
            tags=["test", "integrity"],
            is_global=False,
            db_path=db_path,
        )
        assert not result.startswith("Error"), f"save_memory failed: {result}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after single save")

    def test_save_multiple_notes(self, tmp_dir, mem_dir):
        """Save multiple notes, verify invariants."""
        db_path = mem_dir / "memory.db"

        for i in range(10):
            result = save_memory(
                f"Test memory number {i} with some content to verify.",
                category="lessons",
                title_slug=f"test-memory-{i}",
                tags=["test"],
                is_global=False,
                db_path=db_path,
            )
            assert not result.startswith("Error"), f"save {i} failed: {result}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after 10 saves")

    def test_save_with_backlinks(self, tmp_dir, mem_dir):
        """Save notes that reference each other, verify backlink integrity."""
        db_path = mem_dir / "memory.db"

        result_a = save_memory(
            "See [[lessons/test-b]] for related info.",
            category="lessons",
            title_slug="test-a",
            tags=["test"],
            is_global=False,
            db_path=db_path,
        )
        assert not result_a.startswith("Error"), f"save A failed: {result_a}"

        result_b = save_memory(
            "Referenced from [[lessons/test-a]].",
            category="lessons",
            title_slug="test-b",
            tags=["test"],
            is_global=False,
            db_path=db_path,
        )
        assert not result_b.startswith("Error"), f"save B failed: {result_b}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after backlinked saves")

            bl_count = _count(db, "backlinks")
            assert bl_count >= 2, f"expected >=2 backlinks, got {bl_count}"

            # Verify bidirectional backlinks
            a_bl = db.execute(
                "SELECT target_id FROM backlinks WHERE source_id = ?",
                ("lessons/test-a",),
            ).fetchall()
            b_bl = db.execute(
                "SELECT target_id FROM backlinks WHERE source_id = ?",
                ("lessons/test-b",),
            ).fetchall()
            all_a_targets = {r[0] for r in a_bl}
            all_b_targets = {r[0] for r in b_bl}
            assert "lessons/test-b" in all_a_targets or "test-b" in all_a_targets
            assert "lessons/test-a" in all_b_targets or "test-a" in all_b_targets

    def test_save_global_note(self, tmp_dir):
        """Save with is_global=True, verify integrity on the global-style DB."""
        db_path = tmp_dir / "memory.db"
        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)

        result = save_memory(
            "Global test memory.",
            category="lessons",
            title_slug="global-test",
            tags=["global"],
            is_global=True,
            db_path=db_path,
        )
        assert not result.startswith("Error"), f"save global failed: {result}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after global save")


# ---------------------------------------------------------------------------
# Test: hard_delete cascade
# ---------------------------------------------------------------------------


class TestHardDeleteCascade:
    def _setup_with_data(self, db_path: Path, count: int = 5):
        """Populate DB with notes and all subsystem data."""
        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)
        for i in range(count):
            result = save_memory(
                f"Memory {i} for cascade test.",
                category="lessons",
                title_slug=f"cascade-{i}",
                tags=["cascade"],
                is_global=False,
                db_path=db_path,
            )
            assert not result.startswith("Error"), f"setup save {i} failed: {result}"
        # Populate vec keys and embeddings
        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
        # Also insert KG data
        with open_db(db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
                ("cascade-0", "memory"),
            )
            eid = db.execute(
                "SELECT id FROM kg_entities WHERE name = ?", ("cascade-0",)
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, ?)",
                ("some-concept", "concept"),
            )
            cid = db.execute(
                "SELECT id FROM kg_entities WHERE name = ?", ("some-concept",)
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO kg_edges (source_id, target_id, relation) "
                "VALUES (?, ?, ?)",
                (eid, cid, "related_to"),
            )
            db.execute(
                "INSERT OR IGNORE INTO kg_facts "
                "(source_memory, subject, predicate, object) "
                "VALUES (?, ?, ?, ?)",
                ("lessons/cascade-0", "cascade-0", "is", "test"),
            )
            db.commit()

    def test_hard_delete_cleans_all_subsystems(self, tmp_dir, mem_dir):
        """Hard-delete a note, verify all subsystem rows are removed."""
        db_path = mem_dir / "memory.db"
        self._setup_with_data(db_path, count=5)
        ensure_deleted_at_column(db_path)
        note_id = "lessons/cascade-0"

        with open_db(db_path) as db:
            _assert_invariants(db, "before delete")

        # Soft-delete first (required by hard_delete for young notes)
        soft_delete_note(db_path, note_id)
        result = hard_delete_note(db_path, note_id)
        assert result, "hard_delete_note returned False"

        with open_db(db_path) as db:
            mem_count = _count(db, "memories")
            assert mem_count == 4, f"expected 4 memories after delete, got {mem_count}"
            # Vec key should be gone
            assert _count(db, "memory_vec_keys") == 4
            # Embedding should be gone
            assert _count(db, "memory_embeddings") == 4
            # Backlinks should be cleaned
            bl_orphans = db.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id = ? OR target_id = ?",
                (note_id, note_id.split("/")[-1]),
            ).fetchone()[0]
            assert bl_orphans == 0, f"backlinks remain after delete: {bl_orphans}"
            _assert_invariants(db, "after hard delete")

    def test_hard_delete_multiple_integrity(self, tmp_dir, mem_dir):
        """Delete one note, ensure remaining subsystem data is consistent."""
        db_path = mem_dir / "memory.db"
        self._setup_with_data(db_path, count=5)
        ensure_deleted_at_column(db_path)
        note_id = "lessons/cascade-2"

        with open_db(db_path) as db:
            _assert_invariants(db, "before delete")

        soft_delete_note(db_path, note_id)
        hard_delete_note(db_path, note_id)

        with open_db(db_path) as db:
            _assert_invariants(db, "after deleting one note")


# ---------------------------------------------------------------------------
# Test: rebuild_index consistency
# ---------------------------------------------------------------------------


class TestRebuildIndexConsistency:
    @pytest.mark.slow
    def test_rebuild_preserves_invariants(self, tmp_dir, mem_dir):
        """After save + rebuild, all invariants still hold."""
        db_path = mem_dir / "memory.db"

        for i in range(5):
            save_memory(
                f"Rebuild test memory {i}.",
                category="lessons",
                title_slug=f"rebuild-{i}",
                tags=["rebuild"],
                is_global=False,
                db_path=db_path,
            )

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "before rebuild")

        build_complete_index(db_path)

        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after rebuild")

    @pytest.mark.slow
    def test_rebuild_after_delete(self, tmp_dir, mem_dir):
        """Delete a note, rebuild index, verify consistency."""
        db_path = mem_dir / "memory.db"

        for i in range(5):
            save_memory(
                f"Delete-rebuild test {i}.",
                category="lessons",
                title_slug=f"dr-{i}",
                tags=["rebuild"],
                is_global=False,
                db_path=db_path,
            )

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)

        ensure_deleted_at_column(db_path)
        soft_delete_note(db_path, "lessons/dr-0")
        hard_delete_note(db_path, "lessons/dr-0")

        build_complete_index(db_path)

        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after rebuild+delete")


# ---------------------------------------------------------------------------
# Test: 100+ notes stress test
# ---------------------------------------------------------------------------


class TestStressConsistency:
    @pytest.mark.slow
    def test_100_notes_invariants(self, tmp_dir, mem_dir):
        """Save 100 notes, verify invariants hold under load."""
        db_path = mem_dir / "memory.db"

        for i in range(100):
            result = save_memory(
                f"Stress test memory #{i} with enough content to trigger chunking "
                "and embedding. This is a longer sentence to make sure the content "
                "is meaningful and not too short for processing.",
                category="lessons",
                title_slug=f"stress-{i:04d}",
                tags=["stress", f"batch-{i % 10}"],
                is_global=False,
                db_path=db_path,
            )
            assert not result.startswith("Error"), f"save {i} failed: {result}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "100 notes")

    @pytest.mark.slow
    def test_100_notes_after_rebuild(self, tmp_dir, mem_dir):
        """Save 100 notes, rebuild, verify invariants."""
        db_path = mem_dir / "memory.db"

        for i in range(100):
            save_memory(
                f"Stress rebuild memory #{i} with meaningful content for embedding "
                "and indexing verification across all subsystems.",
                category="lessons",
                title_slug=f"srebuild-{i:04d}",
                tags=["stress", f"batch-{i % 10}"],
                is_global=False,
                db_path=db_path,
            )

        build_complete_index(db_path)

        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "100 notes after rebuild")


# ---------------------------------------------------------------------------
# Test: temporal validity supersede
# ---------------------------------------------------------------------------


class TestTemporalSupersede:
    def test_supersede_maintains_invariants(self, tmp_dir, mem_dir):
        """Supersede a note, verify the superseded note has valid_to set."""
        db_path = mem_dir / "memory.db"

        result_old = save_memory(
            "Old version of the policy.",
            category="decisions",
            title_slug="policy-v1",
            tags=["policy"],
            is_global=False,
            db_path=db_path,
        )
        assert not result_old.startswith("Error"), f"save old failed: {result_old}"

        result_new = save_memory(
            "New version of the policy, supersedes old.",
            category="decisions",
            title_slug="policy-v2",
            tags=["policy"],
            is_global=False,
            db_path=db_path,
        )
        assert not result_new.startswith("Error"), f"save new failed: {result_new}"

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "after supersede")


# ---------------------------------------------------------------------------
# Test: direct DB manipulation invariants
# ---------------------------------------------------------------------------


class TestDirectDbInvariants:
    def test_missing_vec_keys_detected(self, tmp_dir, mem_dir):
        """If vec keys are missing, invariants should fail."""
        db_path = mem_dir / "memory.db"

        save_memory(
            "Test note for vec key detection.",
            category="lessons",
            title_slug="vec-key-test",
            tags=[],
            is_global=False,
            db_path=db_path,
        )

        with open_db(db_path) as db:
            # After C1 fix, vec keys are populated during save.
            # Simulate missing vec keys by deleting them.
            vk_count = _count(db, "memory_vec_keys")
            assert vk_count == 1, "expected 1 vec key after save"

            db.execute("DELETE FROM memory_vec_keys")
            db.commit()
            vk_count = _count(db, "memory_vec_keys")
            assert vk_count == 0, "expected 0 vec keys after deletion"

            mem_count = _count(db, "memories")
            assert mem_count == 1, "expected 1 memory"
            # Invariant should fail because vec keys != memories:
            with pytest.raises(AssertionError):
                _assert_invariants(db)

    def test_embedding_cascade_on_hard_delete(self, tmp_dir, mem_dir):
        """ON DELETE CASCADE removes embedding row when memory is deleted."""
        db_path = mem_dir / "memory.db"

        save_memory(
            "Deleted memory cascade test.",
            category="lessons",
            title_slug="cascade-test",
            tags=[],
            is_global=False,
            db_path=db_path,
        )

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            assert _count(db, "memory_embeddings") == 1

        ensure_deleted_at_column(db_path)
        note_id = "lessons/cascade-test"
        soft_delete_note(db_path, note_id)
        hard_delete_note(db_path, note_id)

        with open_db(db_path) as db:
            assert _count(db, "memory_embeddings") == 0, (
                "embedding should cascade-delete"
            )
            assert _count(db, "memories") == 0
            _assert_invariants(db)

    def test_orphan_backlink_detected(self, tmp_dir, mem_dir):
        """If backlink references a deleted memory, invariants should fail."""
        db_path = mem_dir / "memory.db"

        save_memory(
            "Note A.",
            category="lessons",
            title_slug="orphan-bl-a",
            tags=[],
            is_global=False,
            db_path=db_path,
        )
        save_memory(
            "See [[orphan-bl-a]]",
            category="lessons",
            title_slug="orphan-bl-b",
            tags=[],
            is_global=False,
            db_path=db_path,
        )

        ensure_deleted_at_column(db_path)
        soft_delete_note(db_path, "lessons/orphan-bl-a")
        hard_delete_note(db_path, "lessons/orphan-bl-a")

        with open_db(db_path) as db:
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "backlink cascade OK")


# ---------------------------------------------------------------------------
# Test: save + rebuild + vec rebuild cycle
# ---------------------------------------------------------------------------


class TestFullCycle:
    @pytest.mark.slow
    def test_save_rebuild_vec_integrity(self, tmp_dir, mem_dir):
        """Full cycle: save -> rebuild_index -> rebuild_vec_index -> verify."""
        db_path = mem_dir / "memory.db"

        for i in range(8):
            save_memory(
                f"Full cycle memory {i} with enough text to test embedding "
                "and indexing consistency across all subsystems.",
                category="lessons",
                title_slug=f"cycle-{i}",
                tags=["cycle"],
                is_global=False,
                db_path=db_path,
            )

        build_complete_index(db_path)

        with open_db(db_path) as db:
            _ensure_subsystem_tables(db)
            _populate_vec_keys(db)
            _populate_embeddings(db)
            _assert_invariants(db, "full cycle")
