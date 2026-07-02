#!/usr/bin/env python3
"""One-shot backfill: clean up orphaned KG, chunks, embeddings, and vec_keys.

Runs the same cascade logic as hard_delete_note() across the entire DB:
  1. Delete edges referencing entities with no remaining edges or facts
  2. Delete facts whose source_memory points to deleted notes
  3. Delete orphaned entities
  4. Delete chunks whose parent_id points to deleted notes
  5. Delete embeddings whose memory_id points to deleted notes
  6. Delete vec_keys whose memory_id points to deleted notes

Safe to run multiple times (idempotent).
"""

import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


try:
    from infra._lazy_imports import get_memory_paths, safe_close_db
except ImportError:
    get_memory_paths = None
    safe_close_db = None


def cleanup(conn: AnyConnection) -> dict:
    counts = {}

    # 1. Facts referencing deleted notes
    try:
        r = conn.execute("""
            DELETE FROM kg_facts WHERE source_memory IN (
                SELECT f.source_memory FROM kg_facts f
                LEFT JOIN memories m ON m.id = f.source_memory
                WHERE m.id IS NULL
            )
        """)
        counts["kg_facts_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["kg_facts_deleted"] = 0

    # 2. Edges referencing non-existent entities
    try:
        r = conn.execute("""
            DELETE FROM kg_edges WHERE source_id NOT IN (SELECT id FROM kg_entities)
                OR target_id NOT IN (SELECT id FROM kg_entities)
        """)
        counts["kg_edges_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["kg_edges_deleted"] = 0

    # 3. Orphaned entities (no remaining edges and no remaining facts referencing them)
    try:
        r = conn.execute("""
            DELETE FROM kg_entities WHERE id IN (
                SELECT e.id FROM kg_entities e
                WHERE NOT EXISTS (SELECT 1 FROM kg_edges e2 WHERE e2.source_id = e.id OR e2.target_id = e.id)
                AND NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
            )
        """)
        counts["kg_entities_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["kg_entities_deleted"] = 0

    # 4. Chunks for deleted notes
    try:
        r = conn.execute("""
            DELETE FROM memory_chunks WHERE parent_id IN (
                SELECT mc.parent_id FROM memory_chunks mc
                LEFT JOIN memories m ON m.id = mc.parent_id
                WHERE m.id IS NULL
            )
        """)
        counts["chunks_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["chunks_deleted"] = 0

    # 5. Embeddings for deleted notes
    try:
        r = conn.execute("""
            DELETE FROM memory_embeddings WHERE memory_id IN (
                SELECT me.memory_id FROM memory_embeddings me
                LEFT JOIN memories m ON m.id = me.memory_id
                WHERE m.id IS NULL
            )
        """)
        counts["embeddings_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["embeddings_deleted"] = 0

    # 6. Vec keys for deleted notes
    try:
        r = conn.execute("""
            DELETE FROM memory_vec_keys WHERE memory_id IN (
                SELECT mv.memory_id FROM memory_vec_keys mv
                LEFT JOIN memories m ON m.id = mv.memory_id
                WHERE m.id IS NULL
            )
        """)
        counts["vec_keys_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["vec_keys_deleted"] = 0

    # 7. FTS orphans — entries in FTS5 whose rowid has no matching
    #    memories row, or whose memories row is soft-deleted.
    try:
        r = conn.execute("""
            DELETE FROM memories_fts WHERE rowid IN (
                SELECT fts.rowid FROM memories_fts fts
                LEFT JOIN memories m ON m.rowid = fts.rowid
                WHERE m.rowid IS NULL OR m.deleted_at IS NOT NULL
            )
        """)
        counts["fts_orphans_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["fts_orphans_deleted"] = 0

    # 8. Chunk FTS orphans
    try:
        r = conn.execute("""
            DELETE FROM memory_chunks_fts WHERE rowid IN (
                SELECT fts.rowid FROM memory_chunks_fts fts
                LEFT JOIN memory_chunks mc ON mc.id = fts.rowid
                WHERE mc.id IS NULL
            )
        """)
        counts["chunk_fts_orphans_deleted"] = r.rowcount
    except sqlite3.Error:
        counts["chunk_fts_orphans_deleted"] = 0

    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return counts


def main():
    if get_memory_paths is not None:
        cwd, local_mem, global_mem = get_memory_paths()
        db_path = global_mem / "memory.db"
        if not db_path.exists():
            db_path = local_mem / "memory.db"
    else:
        db_path = Path.home() / ".config" / "agentic-memory" / "memory" / "memory.db"
        if not db_path.exists():
            db_path = Path.cwd() / "memory" / "memory.db"
    if not db_path.exists():
        print("No memory.db found")
        sys.exit(1)

    print(f"Cleaning: {db_path}")
    from infra._lazy_imports import connection_pool

    conn = connection_pool.get(str(db_path), timeout=30.0)
    try:
        counts = cleanup(conn)
    finally:
        if safe_close_db is not None:
            safe_close_db(conn)
    for k, v in counts.items():
        print(f"  {k}: {v}")
    total = sum(counts.values())
    print(f"  TOTAL: {total} orphaned rows removed")


if __name__ == "__main__":
    main()
