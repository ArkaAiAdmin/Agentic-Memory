"""Per-index backfill functions for the backfill pipeline.

Extracted from backfill_all.py (2026-06-20) as part of the god-module
decomposition. Contains the per-index rebuild functions that
``backfill_full`` and ``backfill_incremental`` call:

- _backfill_memories_from_markdown: load memories from .md files
- _backfill_fts: rebuild memories_fts from memories
- _backfill_embeddings: encode all memories into memory_embeddings
- _backfill_chunks: create memory_chunks for memories lacking them
- _backfill_chunks_fts: populate memory_chunks_fts from memory_chunks
- _backfill_backlinks: rebuild backlinks from [[wiki-links]]
- _backfill_vec_index_raw: build usearch vector index
- _backfill_crdt_vectors: backfill version vectors and logical clocks
- _backfill_tiers: backfill tier assignments

Behavior is identical to the inline versions. Re-exported from
backfill_all for backward compat.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _backfill_memories_from_markdown(conn, source_dir: Path, db_path: Path):
    """Scan markdown files and populate memories table (lightweight version)."""
    if not source_dir.exists():
        logger.warning("Source dir %s does not exist", source_dir)
        return

    # Count existing
    existing = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    if existing > 0:
        return  # Already populated

    # Delegate to rebuild_index for full schema + markdown scan
    try:
        from rebuild_index import rebuild_index

        rebuild_index(str(source_dir), str(db_path))
    except Exception as e:
        logger.error("Could not rebuild memories from markdown: %s", e)


def _backfill_fts(conn):
    """Rebuild memories_fts from memories table."""
    try:
        conn.execute("DELETE FROM memories_fts")
    except Exception:
        # FTS5 virtual table doesn't exist — create it
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id, content, tags, category, tokenize='porter unicode61')"
            )
        except Exception as e:
            logger.warning("Cannot create memories_fts: %s", e)
            return
    rows = conn.execute(
        "SELECT rowid, id, content, tags, category FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    for rowid, mem_id, content, tags, category in rows:
        if content:
            try:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, id, content, tags, category) VALUES (?, ?, ?, ?, ?)",
                    (rowid, mem_id, content, tags or "", category or ""),
                )
            except Exception as e:
                logger.debug("FTS insert failed for rowid %s: %s", rowid, e)
    logger.info("FTS backfilled: %d rows", len(rows))


def _backfill_embeddings(conn):
    """Batch-encode all memories into memory_embeddings."""
    try:
        from embedding_search import get_embedding_search

        es = get_embedding_search()
        if es.model is None:
            logger.warning("Embedding model unavailable — skipping embeddings backfill")
            return
    except Exception as e:
        logger.warning("Cannot load embedding model: %s", e)
        return

    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    if not rows:
        return

    items = [(mid, content) for mid, content in rows if content]
    written = es.index_embeddings_batch(conn, items)
    logger.info("Embeddings backfilled: %d rows", written)


def _backfill_chunks(conn):
    """Create memory_chunks for all memories that lack them."""
    try:
        from search.chunk_index import _qw5_index_chunks_for
    except ImportError:
        try:
            from search_pipeline import _qw5_index_chunks_for
        except ImportError:
            logger.warning("Cannot import _qw5_index_chunks_for — skipping chunks")
            return

    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    count = 0
    for mem_id, content in rows:
        if not content:
            continue
        existing = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE parent_id = ?", (mem_id,)
        ).fetchone()[0]
        if existing == 0:
            try:
                _qw5_index_chunks_for(conn, mem_id, content)
                count += 1
            except Exception as e:
                logger.warning("Chunk backfill failed for %s: %s", mem_id, e)
    logger.info("Chunks backfilled: %d memories processed", count)


def _backfill_chunks_fts(conn):
    """Populate memory_chunks_fts from memory_chunks."""
    try:
        existing = conn.execute("SELECT COUNT(*) FROM memory_chunks_fts").fetchone()[0]
    except Exception:
        return
    if existing > 0:
        return

    rows = conn.execute("SELECT id, content FROM memory_chunks").fetchall()
    for cid, content in rows:
        if content:
            try:
                conn.execute(
                    "INSERT INTO memory_chunks_fts(rowid, content) VALUES (?, ?)",
                    (cid, content),
                )
            except Exception:
                pass
    logger.info("Chunks FTS backfilled: %d rows", len(rows))


def _backfill_backlinks(conn):
    """Rebuild backlinks from [[wiki-links]] in memory content."""
    try:
        existing = conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0]
    except Exception:
        return
    if existing > 0:
        return

    rows = conn.execute(
        "SELECT id, content, source_file FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    all_ids = {r[0] for r in rows}
    count = 0
    for mid, content, source_file in rows:
        if not content:
            continue
        links = re.findall(r"\[\[(.*?)\]\]", content)
        for link in links:
            target = (
                link.split("|")[0].strip().replace(".md", "").lower().replace("\\", "/")
            )
            if target in all_ids and target != mid:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                        (mid, target),
                    )
                    count += 1
                except Exception:
                    pass
    logger.info("Backlinks backfilled: %d links", count)


def _backfill_vec_index_raw(db_path: Path):
    """Build usearch vector index from memory_embeddings.

    Runs outside the connection pool to avoid lock conflicts —
    rebuild_vec_index opens its own connections internally.
    """
    try:
        from rebuild_vec_index import rebuild_vec_index

        stats = rebuild_vec_index(str(db_path))
        logger.info("Vector index rebuilt: %s", stats.get("n_indexed", 0))
    except Exception as e:
        logger.warning("Vector index rebuild failed: %s", e)


def _backfill_crdt_vectors(conn: sqlite3.Connection) -> dict:
    """Backfill version vectors and logical clocks for memories lacking them."""

    missing = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE version_vector = '{}' AND logical_clock = 0"
    ).fetchone()[0]

    if missing == 0:
        return {"result": "ok", "count": 0}

    from config import get_config

    cfg = get_config()
    agent_id = cfg.agent_id or os.environ.get("MEMORY_AGENT_ID") or "local"
    count = 0

    rows = conn.execute(
        "SELECT id, created_at FROM memories WHERE version_vector = '{}' AND logical_clock = 0"
    ).fetchall()

    for note_id, created_at in rows:
        vv = json.dumps({agent_id: 1})
        conn.execute(
            "UPDATE memories SET version_vector = ?, logical_clock = 1 WHERE id = ?",
            (vv, note_id),
        )
        count += 1

    logger.info("CRDT vectors backfilled: %d/%d memories", count, missing)
    return {"result": "completed", "backfilled": count, "total_missing": missing}


def _backfill_tiers(conn: sqlite3.Connection) -> dict:
    """Backfill tier assignments for memories with NULL tier."""
    from self_directed import _assign_tier, compute_importance

    missing = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE tier IS NULL"
    ).fetchone()[0]

    if missing == 0:
        return {"result": "ok", "count": 0}

    count = 0
    hots = warms = colds = 0
    rows = conn.execute("SELECT id FROM memories WHERE tier IS NULL").fetchall()

    for (note_id,) in rows:
        importance = compute_importance(conn, note_id)
        tier = _assign_tier(importance)
        try:
            conn.execute(
                "UPDATE memories SET tier = ?, importance_score = ? WHERE id = ?",
                (tier, importance, note_id),
            )
            count += 1
            if tier == "hot":
                hots += 1
            elif tier == "warm":
                warms += 1
            else:
                colds += 1
        except Exception:
            pass

    logger.info(
        "Tiers backfilled: %d/%d (hot=%d warm=%d cold=%d)",
        count,
        missing,
        hots,
        warms,
        colds,
    )
    return {
        "result": "completed",
        "backfilled": count,
        "total_missing": missing,
        "hot": hots,
        "warm": warms,
        "cold": colds,
        "skipped": missing - count,
    }
