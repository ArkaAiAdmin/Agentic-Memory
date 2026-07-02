"""Database migration helpers extracted to break the circular import
between ``db.py`` and ``memory_common.py``.

Both modules import from here, eliminating the cycle:
  db.py ──lazy──> db_migrations.py <──module-level── memory_common.py

All names that used to live in ``memory_common.py`` are re-exported
there so ``from memory_common import _migrate_*`` still works for the
93+ dependents.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Module-level set tracking which connection ids have had their schema
# initialised. Used by ``run_schema_setup`` as a fast-path gate; cannot
# _MIGRATIONS_DONE cache removed (2026-06-16) — it was keyed by id(conn)
# which Python recycles after GC, causing new conns to read stale
# "migrations done" entries and skip the schema setup. The schema_version
# SELECT in run_schema_setup is the correct fast-path. See
# lessons/bug-migrations-done-cache-recycled-ids-2026-06-16.

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

# ---------------------------------------------------------------------------
# SCHEMA_VERSION
# ---------------------------------------------------------------------------
# Single source of truth: imported by db.py, db_migrations.py, and
# migration_runner.py. Mutating a schema must bump this AND add a
# matching migrations/NNN_*.sql file (see migration_runner._get_available_migrations).
from infra.migration_runner import SCHEMA_VERSION  # noqa: F401  re-export

# ---------------------------------------------------------------------------
# Non-FTS5 schema migration helpers
# ---------------------------------------------------------------------------


def _migrate_schema_version(conn: 'AnyConnection') -> None:
    """Create the schema_version singleton and stamp it.

    Must run FIRST in the migration chain so the fast-path gate in
    ``run_db_migrations`` is in place for the very next open.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  id      INTEGER PRIMARY KEY CHECK (id = 1),"
        "  version INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
        (SCHEMA_VERSION,),
    )


def _migrate_memory_embeddings(conn: 'AnyConnection') -> None:
    """Create the memory_embeddings cache table if absent."""
    conn.execute(
        "\n        CREATE TABLE IF NOT EXISTS memory_embeddings (\n"
        "            memory_id       TEXT PRIMARY KEY,\n"
        "            content_hash    TEXT NOT NULL,\n"
        "            embedding       BLOB NOT NULL,\n"
        "            model_revision  TEXT NOT NULL,\n"
        "            dim             INTEGER NOT NULL,\n"
        "            updated_at      REAL NOT NULL,\n"
        "            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE\n"
        "        )\n"
        "        "
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hash ON memory_embeddings(content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_revision ON memory_embeddings(model_revision)"
    )


def _migrate_memory_audit_log(conn: 'AnyConnection') -> None:
    """Create the memory_audit_log table if absent (Sprint 4 / P0 #4)."""
    conn.execute(
        "\n        CREATE TABLE IF NOT EXISTS memory_audit_log (\n"
        "            id              INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "            ts              REAL    NOT NULL,\n"
        "            tool            TEXT    NOT NULL,\n"
        "            args            TEXT,\n"
        "            results_count   INTEGER,\n"
        "            top1_id         TEXT,\n"
        "            latency_ms      REAL    NOT NULL,\n"
        "            error           TEXT,\n"
        "            request_id      TEXT\n"
        "        )\n"
        "        "
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_tool_ts ON memory_audit_log(tool, ts)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON memory_audit_log(ts)")


def _migrate_memory_vec_idx(conn: 'AnyConnection') -> None:
    """Create the memory_vec_idx + memory_vec_keys tables (Sprint 4 / P2 #8)."""
    conn.execute(
        "\n        CREATE TABLE IF NOT EXISTS memory_vec_idx (\n"
        "            id                INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "            n_vectors         INTEGER NOT NULL,\n"
        "            dim               INTEGER NOT NULL,\n"
        "            metric            TEXT    NOT NULL,\n"
        "            quantization      TEXT    NOT NULL,\n"
        "            connectivity      INTEGER NOT NULL,\n"
        "            expansion_add     INTEGER NOT NULL,\n"
        "            expansion_search  INTEGER NOT NULL,\n"
        "            built_at          REAL    NOT NULL,\n"
        "            index_blob        BLOB    NOT NULL,\n"
        "            key_count         INTEGER NOT NULL\n"
        "        )\n"
        "        "
    )
    conn.execute(
        "\n        CREATE TABLE IF NOT EXISTS memory_vec_keys (\n"
        "            key         INTEGER PRIMARY KEY,\n"
        "            memory_id   TEXT    NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE\n"
        "        )\n"
        "        "
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vec_keys_memory_id ON memory_vec_keys(memory_id)"
    )


def _migrate_ensure_columns(conn: 'AnyConnection', existing_cols: set) -> None:
    """Idempotently add missing columns to the memories table."""
    desired = (
        ("valid_from", "TEXT"),
        ("valid_to", "TEXT"),
        ("superseded_by", "TEXT"),
        ("last_accessed", "TEXT"),
        ("deleted_at", "TEXT"),
        ("deleted_by", "TEXT"),
        ("context_prefix", "TEXT"),
        ("category", "TEXT"),
        ("tier", "TEXT"),
        ("importance_score", "REAL"),
        ("metadata", "TEXT"),
        ("tenant_id", "TEXT DEFAULT 'default'"),
    )
    for col_name, col_type in desired:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass


def _migrate_ensure_backlinks_table(conn: 'AnyConnection') -> None:
    """Create the backlinks table for bidirectional wiki-links and semantic edges."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backlinks (
                source_id TEXT,
                target_id TEXT,
                PRIMARY KEY (source_id, target_id)
            )
        """
        )
    except sqlite3.OperationalError:
        pass


def _migrate_ensure_indexes(conn: 'AnyConnection') -> None:
    """Create performance indexes if missing. Idempotent."""
    _migrate_ensure_backlinks_table(conn)
    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_memories_repo_id ON memories(repo_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned)",
        "CREATE INDEX IF NOT EXISTS idx_memories_consolidation_state ON memories(consolidation_state)",
        "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_observed_at ON memories(observed_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_fitness_score ON memories(fitness_score)",
        "CREATE INDEX IF NOT EXISTS idx_memories_source_file ON memories(source_file)",
        "CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_valid_to ON memories(valid_to)",
        "CREATE INDEX IF NOT EXISTS idx_memories_valid_from ON memories(valid_from)",
        "CREATE INDEX IF NOT EXISTS idx_memories_superseded_by ON memories(superseded_by)",
        "CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories(last_accessed)",
        "CREATE INDEX IF NOT EXISTS idx_memories_deleted_at ON memories(deleted_at)",
    )
    for stmt in indexes:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass


def _migrate_memory_ctr_feedback(conn: 'AnyConnection') -> None:
    """Create CTR feedback table if absent (P2a)."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_ctr_feedback (
                id            TEXT PRIMARY KEY,
                query_id      TEXT NOT NULL,
                returned_at   REAL NOT NULL,
                clicked_at    REAL,
                dismissed_at  REAL,
                source        TEXT,
                ranking_params TEXT
            )
        """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ctr_query_id ON memory_ctr_feedback(query_id)"
        )
    except sqlite3.OperationalError:
        pass


def _migrate_concept_drift(conn: 'AnyConnection') -> None:
    """Create concept drift tracking table if absent.

    2026-06-22 (D1 fix): the canonical schema is now in
    `migrations/016_concept_drift.sql` and is applied by
    `migration_runner`. This Python helper is a safety net so that
    code paths which open a DB that pre-dates migration 016 still get
    a working `concept_drift` table. The CREATE statements here must
    match `migrations/016_concept_drift.sql` exactly — they are
    intentionally idempotent (`IF NOT EXISTS`) so running this
    helper against a v16 DB is a no-op.
    """
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS concept_drift (
                id               TEXT PRIMARY KEY,
                drift_metric     REAL NOT NULL,
                drifted_dimensions TEXT,
                triggered_at     REAL NOT NULL,
                acknowledged     INTEGER DEFAULT 0
            )
        """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_drift_triggered ON concept_drift(triggered_at)"
        )
    except sqlite3.OperationalError:
        pass


def _migrate_add_fk_constraints(conn: 'AnyConnection') -> None:
    """Add missing FK constraints to tables that were created without them.

    Recreates tables with FK constraints if they don't already have them.
    Uses a transaction to ensure atomicity.
    """
    # Exclude backlinks because it must allow target_id to refer to non-existent notes
    fk_definitions = [
        ("memory_chunks", "parent_id", "memories", "id", "CASCADE"),
        ("kg_facts", "source_memory", "memories", "id", "SET NULL"),
        ("shared_memories", "source_note_id", "memories", "id", "SET NULL"),
        ("user_profile_access_log", "note_id", "memories", "id", "CASCADE"),
    ]

    # Clean up backlinks if it was previously migrated with FK constraints
    try:
        fks = conn.execute("PRAGMA foreign_key_list(backlinks)").fetchall()
        if fks:
            conn.execute("DROP TABLE IF EXISTS backlinks_backup")
            conn.execute("CREATE TABLE backlinks_backup AS SELECT * FROM backlinks")
            conn.execute("DROP TABLE backlinks")
            conn.execute("""
                CREATE TABLE backlinks (
                    source_id TEXT,
                    target_id TEXT,
                    PRIMARY KEY (source_id, target_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id)"
            )
            conn.execute("INSERT INTO backlinks SELECT * FROM backlinks_backup")
            conn.execute("DROP TABLE backlinks_backup")
    except sqlite3.OperationalError:
        pass

    table_schemas = {
        "memory_chunks": """
            CREATE TABLE memory_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(parent_id, chunk_idx),
                FOREIGN KEY (parent_id) REFERENCES memories(id) ON DELETE CASCADE
            )
        """,
        "kg_facts": """
            CREATE TABLE kg_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                locked INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                mention_count INTEGER DEFAULT 1,
                source_memory TEXT,
                context TEXT,
                UNIQUE(subject, predicate, object),
                FOREIGN KEY (source_memory) REFERENCES memories(id) ON DELETE SET NULL
            )
        """,
        "shared_memories": """
            CREATE TABLE shared_memories (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT,
                tags TEXT,
                shared_at REAL NOT NULL,
                source_note_id TEXT,
                metadata TEXT,
                FOREIGN KEY (source_note_id) REFERENCES memories(id) ON DELETE SET NULL
            )
        """,
        "user_profile_access_log": """
            CREATE TABLE user_profile_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                source TEXT DEFAULT 'search',
                category TEXT,
                tags TEXT,
                accessed_at REAL NOT NULL,
                FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
            )
        """,
    }

    # Group by table to build all FKs at once
    tables_to_process: dict[str, list[tuple[str, str, str, str]]] = {}
    for table, col, ref_table, ref_col, on_delete in fk_definitions:
        tables_to_process.setdefault(table, []).append(
            (col, ref_table, ref_col, on_delete)
        )

    for table, fk_list in tables_to_process.items():
        # Skip if table doesn't exist yet
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue

        fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        existing_fk_cols = {fk[3] for fk in fks}
        missing = [
            (col, ref_table, ref_col, on_delete)
            for col, ref_table, ref_col, on_delete in fk_list
            if col not in existing_fk_cols
        ]
        if missing:
            # Clean orphaned rows for all missing FK columns
            for col, ref_table, ref_col, on_delete in missing:
                orphans = conn.execute(f"""
                    SELECT COUNT(*) FROM {table} t
                    LEFT JOIN {ref_table} r ON t.{col} = r.{ref_col}
                    WHERE r.{ref_col} IS NULL AND t.{col} IS NOT NULL
                """).fetchone()[0]
                if orphans > 0:
                    print(f"  Cleaning {orphans} orphaned {table}.{col} rows")
                    conn.execute(f"""
                        DELETE FROM {table}
                        WHERE {col} NOT IN (SELECT {ref_col} FROM {ref_table}) AND {col} IS NOT NULL
                    """)

            # Create new table with all FKs
            conn.execute(f"DROP TABLE IF EXISTS {table}_backup")
            conn.execute(f"CREATE TABLE {table}_backup AS SELECT * FROM {table}")
            conn.execute(f"DROP TABLE {table}")

            # Recreate with canonical schema including FKs
            conn.execute(table_schemas[table])

            # Copy data back dynamically by matching columns
            columns = [
                c[1]
                for c in conn.execute(f"PRAGMA table_info({table}_backup)").fetchall()
            ]
            cols_str = ", ".join(columns)
            conn.execute(
                f"INSERT INTO {table} ({cols_str}) SELECT {cols_str} FROM {table}_backup"
            )
            conn.execute(f"DROP TABLE {table}_backup")

            # Recreate indexes
            if table == "memory_chunks":
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent_id ON memory_chunks(parent_id)"
                )
            elif table == "kg_facts":
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object)"
                )
    # B4 fix: removed inner conn.commit(). The outer with conn: in
    # run_schema_setup provides transaction atomicity.


def _migrate_fix_kg_edges_fk(conn: 'AnyConnection') -> None:
    """Fix kg_edges FK ON DELETE from NO ACTION to CASCADE."""
    fks = conn.execute("PRAGMA foreign_key_list(kg_edges)").fetchall()
    needs_fix = any(fk[6] == "NO ACTION" for fk in fks)
    if needs_fix:
        # Get existing data
        rows = conn.execute("SELECT * FROM kg_edges").fetchall()
        conn.execute("DROP TABLE kg_edges")
        conn.execute("""
            CREATE TABLE kg_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
                relation TEXT NOT NULL DEFAULT 'related_to',
                weight REAL DEFAULT 1.0,
                created_at TEXT,
                valid_at TEXT,
                invalid_at TEXT,
                UNIQUE(source_id, target_id, relation)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at)"
        )
        # Copy data back. M30 fix: use named columns so this stays
        # correct if kg_edges ever gets a new column added in a future
        # migration. The column order is whatever the SELECT above
        # returned, so the parameterised INSERT is bound by name.
        cols = [d[0] for d in conn.execute("PRAGMA table_info(kg_edges)").fetchall()]
        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        for row in rows:
            conn.execute(
                f"INSERT INTO kg_edges ({col_list}) VALUES ({placeholders})", row
            )
    # B4 fix: removed inner conn.commit() for atomicity.


def _migrate_ensure_chunks_table(conn: 'AnyConnection') -> None:
    """Create the memory_chunks table for pre-computed semantic chunks."""
    try:
        conn.execute(
            "\n            CREATE TABLE IF NOT EXISTS memory_chunks (\n"
            "                id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "                parent_id TEXT NOT NULL,\n"
            "                chunk_idx INTEGER NOT NULL,\n"
            "                start_offset INTEGER NOT NULL,\n"
            "                end_offset INTEGER NOT NULL,\n"
            "                content TEXT NOT NULL,\n"
            "                created_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
            "                UNIQUE(parent_id, chunk_idx),\n"
            "                FOREIGN KEY (parent_id) REFERENCES memories(id) ON DELETE CASCADE\n"
            "            )\n"
            "        "
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent_id ON memory_chunks(parent_id)"
        )
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_chunks_fts'"
        ).fetchone()
        if not fts_exists:
            conn.execute(
                "\n                CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(\n"
                "                    content, parent_id, chunk_idx,\n"
                "                    content=memory_chunks,\n"
                "                    content_rowid=id,\n"
                "                    tokenize='porter unicode61'\n"
                "                )\n"
                "            "
            )
        existing_triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        if "memory_chunks_ai" not in existing_triggers:
            conn.execute(
                "\n                CREATE TRIGGER IF NOT EXISTS memory_chunks_ai AFTER INSERT ON memory_chunks BEGIN\n"
                "                    INSERT INTO memory_chunks_fts(rowid, content, parent_id, chunk_idx)\n"
                "                    VALUES (new.id, new.content, new.parent_id, new.chunk_idx);\n"
                "                END\n"
                "            "
            )
        if "memory_chunks_ad" not in existing_triggers:
            conn.execute(
                "\n                CREATE TRIGGER IF NOT EXISTS memory_chunks_ad AFTER DELETE ON memory_chunks BEGIN\n"
                "                    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content, parent_id, chunk_idx)\n"
                "                    VALUES ('delete', old.id, old.content, old.parent_id, old.chunk_idx);\n"
                "                END\n"
                "            "
            )
        if "memory_chunks_au" not in existing_triggers:
            conn.execute(
                "\n                CREATE TRIGGER IF NOT EXISTS memory_chunks_au AFTER UPDATE ON memory_chunks BEGIN\n"
                "                    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content, parent_id, chunk_idx)\n"
                "                    VALUES ('delete', old.id, old.content, old.parent_id, old.chunk_idx);\n"
                "                    INSERT INTO memory_chunks_fts(rowid, content, parent_id, chunk_idx)\n"
                "                    VALUES (new.id, new.content, new.parent_id, new.chunk_idx);\n"
                "                END\n"
                "            "
            )
        # B4 fix: removed inner conn.commit() for atomicity.
    except Exception as e:
        import traceback

        traceback.print_exc()
        logger.warning("_migrate_ensure_chunks_table failed: %s", e)


def _migrate_kg_tables(conn: 'AnyConnection') -> None:
    """Create knowledge graph tables and FTS5 indexes if absent."""
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kg_entities ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, entity_type TEXT, "
            "mentions INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT, "
            "UNIQUE(name, entity_type))",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type)",
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kg_edges ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE, "
            "target_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE, "
            "relation TEXT NOT NULL DEFAULT 'related_to', weight REAL DEFAULT 1.0, "
            "created_at TEXT, valid_at TEXT, invalid_at TEXT, "
            "UNIQUE(source_id, target_id, relation))",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at)",
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kg_facts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, "
            "predicate TEXT NOT NULL, object TEXT NOT NULL, confidence REAL DEFAULT 1.0, "
            "locked INTEGER DEFAULT 0, first_seen REAL, last_seen REAL, "
            "mention_count INTEGER DEFAULT 1, source_memory TEXT, context TEXT, "
            "UNIQUE(subject, predicate, object))",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object)"
        )
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_entities_fts'"
        ).fetchone()
        if not fts_exists:
            conn.execute(
                "CREATE VIRTUAL TABLE kg_entities_fts USING fts5("
                "name, entity_type, content='kg_entities', content_rowid='id')",
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ai AFTER INSERT ON kg_entities BEGIN"
                " INSERT INTO kg_entities_fts(rowid, name, entity_type)"
                " VALUES (new.id, new.name, new.entity_type); END",
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ad AFTER DELETE ON kg_entities BEGIN"
                " INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)"
                " VALUES('delete', old.id, old.name, old.entity_type); END",
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kg_entities_fts_au AFTER UPDATE ON kg_entities BEGIN"
                " INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)"
                " VALUES('delete', old.id, old.name, old.entity_type);"
                " INSERT INTO kg_entities_fts(rowid, name, entity_type)"
                " VALUES (new.id, new.name, new.entity_type); END",
            )
            try:
                fts_count = conn.execute(
                    "SELECT COUNT(*) FROM kg_entities_fts"
                ).fetchone()[0]
                ent_count = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[
                    0
                ]
                if fts_count == 0 and ent_count > 0:
                    conn.execute(
                        "INSERT INTO kg_entities_fts(rowid, name, entity_type)"
                        " SELECT id, name, entity_type FROM kg_entities",
                    )
            except Exception:
                logger.warning("Failed to backfill KG FTS during migration")
                pass
        # B4 fix: removed inner conn.commit() for atomicity.
    except Exception as exc:
        logger.warning("_migrate_kg_tables failed: %s", exc)


def _migrate_kg_extraction_stats(conn: 'AnyConnection') -> None:
    """Create the kg_extraction_stats observability table (P2a.2).

    Per-memory observability for the two-stage extraction pipeline
    (regex → LLM fallback). One row per index_kg_for_memory() call.
    The forward migration 012_kg_extraction_stats.sql is the
    authoritative DDL; this Python helper exists as a safety net
    for DBs that have not yet run the SQL migration but do call
    ensure_kg_schema via the pool's _ensure_full_schema path.
    """
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kg_extraction_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                entities_extracted INTEGER DEFAULT 0,
                regex_count INTEGER DEFAULT 0,
                llm_count INTEGER DEFAULT 0,
                duration_ms REAL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_memory"
            " ON kg_extraction_stats(memory_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_created"
            " ON kg_extraction_stats(created_at)"
        )
    except Exception as exc:
        logger.warning("_migrate_kg_extraction_stats failed: %s", exc)


# ---------------------------------------------------------------------------
# run_db_migrations — the top-level migration runner
# ---------------------------------------------------------------------------


def run_schema_setup(conn: 'AnyConnection') -> None:
    """Idempotently bring a memory DB up to the current schema and initialize all subsystems.

    This is the single source of truth for database schema setup and migrations.

    Per-connection FK enforcement: we set ``PRAGMA foreign_keys = ON``
    unconditionally at the top of this function because some legacy
    callers (older scripts in eval/, some embedded tools) connect via
    raw ``sqlite3.connect`` and never set the pragma. Without this guard,
    FK clauses get attached to the schema but are never enforced, so
    ``PRAGMA foreign_key_check`` would have reported bugs that newer
    writes silently violate. (Audit M3 verification.) The pragma is
    per-connection, so re-setting it on every setup call is safe and
    idempotent.
    """
    # Fast-path gate: avoid re-running migrations on an already-set-up DB.
    # We previously used a module-level dict keyed by id(conn), but that
    # is broken: Python recycles object ids after garbage collection, so
    # a new conn2 may read the cached True for a recycled id and skip
    # migrations even though its underlying DB is a different (empty) path.
    # The correct fast-path is the SQL version check below (step 1): if
    # schema_version is already at SCHEMA_VERSION, return. The cost is
    # one extra SELECT per open_db call (~microseconds), which is far
    # cheaper than the bug (potentially missing schema on recycled conns).
    # See: lessons/bug-migrations-done-cache-recycled-ids-2026-06-16

    # Force-enable FK enforcement before any migration runs. See the
    # expanded docstring for why this is non-optional.
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.OperationalError:
        pass

    from infra.migration_runner import run_migrations as _run_sql_migrations

    # 1. SQL migrations version check (the correct fast-path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        if row is not None and row[0] >= SCHEMA_VERSION:
            return
    except sqlite3.OperationalError:
        pass

    # Ensure the core memories table exists.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            source_file   TEXT NOT NULL,
            tags          TEXT DEFAULT '[]',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            observed_at   TEXT NOT NULL,
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
            tenant_id     TEXT DEFAULT 'default'
        )
        """
    )

    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
    except sqlite3.OperationalError:
        return
    if not cols:
        return

    with conn:
        _run_sql_migrations(conn)
        _migrate_ensure_columns(conn, cols)
        _migrate_ensure_indexes(conn)
        _migrate_memory_embeddings(conn)
        _migrate_memory_audit_log(conn)
        _migrate_memory_vec_idx(conn)
        _migrate_kg_tables(conn)
        _migrate_kg_extraction_stats(conn)
        from infra.fts import _migrate_fts5_porter_tokenizer, _migrate_ensure_fts_triggers
        _migrate_fts5_porter_tokenizer(conn)
        _migrate_ensure_fts_triggers(conn)
        _migrate_memory_ctr_feedback(conn)
        _migrate_concept_drift(conn)
        _migrate_add_fk_constraints(conn)
        _migrate_ensure_chunks_table(conn)
        _migrate_fix_kg_edges_fk(conn)
        _migrate_schema_version(conn)

        # 2. Dynamic subsystem setups (best-effort, lazy loaded to prevent circular imports)
        try:
            from adaptive_retention import ensure_adaptive_schema

            ensure_adaptive_schema(conn)
        except Exception:
            logger.warning(
                "Failed to ensure adaptive retention schema during migration"
            )
            pass

        try:
            from memory_sharing import _ensure_shared_table

            _ensure_shared_table(conn)
        except Exception:
            logger.warning("Failed to ensure memory_sharing table during migration")
            pass

        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL,
                    source TEXT DEFAULT 'search',
                    category TEXT,
                    tags TEXT,
                    accessed_at REAL NOT NULL,
                    FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_profile_note ON user_profile_access_log(note_id)"
            )
        except Exception:
            logger.warning(
                "Failed to create user_profile_access_log table during migration"
            )
            pass

    # No need to mark the conn — the schema_version table is the
    # canonical fast-path. See the gate at the top of this function.


def run_db_migrations(conn: 'AnyConnection') -> None:
    """Legacy entry point delegating to the unified run_schema_setup."""
    run_schema_setup(conn)
