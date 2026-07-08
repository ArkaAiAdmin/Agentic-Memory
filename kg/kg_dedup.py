#!/usr/bin/env python3
from __future__ import annotations

import logging
logger = logging.getLogger(__name__)
"""Cron wrapper: kg_dedup — auto-merge duplicate KG entities.

Merges entities with the same normalized name and type, plus fuzzy
semantic matching via embeddings.  Consolidates mentions, merges edges,
removes stale entries.

Usage:
    venv/bin/python kg_dedup.py [db_path] [--semantic] [--threshold=0.92]
"""

__all__ = [
    "dedup_entities",
    "compute_semantic_merge_candidates",
    "merge_entities",
    "main",
]
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from infra.infrastructure import resolve_active_memory_dir
from typing import TYPE_CHECKING, Callable, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection


get_config: Callable[[], Any] | None = None
try:
    from config import get_config as _gc
    get_config = _gc
except Exception as e:
    logger.warning("operation failed: %s", e)


# ---------------------------------------------------------------------------
# Core merge logic (shared by exact and semantic dedup)
# ---------------------------------------------------------------------------


def merge_entities(
    conn: AnyConnection,
    keep_id: int,
    merge_id: int,
    dry_run: bool = False,
) -> dict:
    """Merge *merge_id* into *keep_id*: transfer edges, sum mentions, delete stale.

    Returns {"edges_redirected": N} (always — even in dry_run for reporting).
    """
    keep_row = conn.execute(
        "SELECT mentions FROM kg_entities WHERE id = ?", (keep_id,)
    ).fetchone()
    merge_row = conn.execute(
        "SELECT mentions FROM kg_entities WHERE id = ?", (merge_id,)
    ).fetchone()
    if not keep_row or not merge_row:
        return {"edges_redirected": 0}

    edges_redirected = 0
    if not dry_run:
        # Sum mentions
        total = (keep_row[0] or 0) + (merge_row[0] or 0)
        conn.execute(
            "UPDATE kg_entities SET mentions = ?, updated_at = datetime('now') WHERE id = ?",
            (total, keep_id),
        )
        # Redirect source edges
        for eid, target_id, relation in conn.execute(
            "SELECT id, target_id, relation FROM kg_edges WHERE source_id = ?",
            (merge_id,),
        ).fetchall():
            existing = conn.execute(
                "SELECT id FROM kg_edges "
                "WHERE source_id = ? AND target_id = ? AND relation = ?",
                (keep_id, target_id, relation),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE kg_edges SET weight = weight + 0.1 WHERE id = ?",
                    (existing[0],),
                )
                conn.execute("DELETE FROM kg_edges WHERE id = ?", (eid,))
            else:
                conn.execute(
                    "UPDATE kg_edges SET source_id = ? WHERE id = ?",
                    (keep_id, eid),
                )
            edges_redirected += 1
        # Redirect target edges
        for eid, source_id, relation in conn.execute(
            "SELECT id, source_id, relation FROM kg_edges WHERE target_id = ?",
            (merge_id,),
        ).fetchall():
            existing = conn.execute(
                "SELECT id FROM kg_edges "
                "WHERE source_id = ? AND target_id = ? AND relation = ?",
                (source_id, keep_id, relation),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE kg_edges SET weight = weight + 0.1 WHERE id = ?",
                    (existing[0],),
                )
                conn.execute("DELETE FROM kg_edges WHERE id = ?", (eid,))
            else:
                conn.execute(
                    "UPDATE kg_edges SET target_id = ? WHERE id = ?",
                    (keep_id, eid),
                )
            edges_redirected += 1
        # Delete stale entity and any orphaned edges
        conn.execute(
            "DELETE FROM kg_edges WHERE source_id = ? OR target_id = ?",
            (merge_id, merge_id),
        )
        conn.execute("DELETE FROM kg_entities WHERE id = ?", (merge_id,))

    return {"edges_redirected": edges_redirected}


# ---------------------------------------------------------------------------
# Exact dedup (existing behavior)
# ---------------------------------------------------------------------------


def dedup_entities(conn: AnyConnection, dry_run: bool = False) -> dict:
    """Find and merge duplicate KG entities by exact name+type match.

    Strategy: group by (name, entity_type), keep the one with highest
    id (newest), merge mentions from all duplicates into it, redirect
    all edges to the kept entity, delete the stale rows.

    Returns: {"groups_found": N, "entities_merged": N, "edges_redirected": N}
    """
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_entities'"
        ).fetchall()
    ]
    if not tables:
        return {
            "groups_found": 0,
            "entities_merged": 0,
            "edges_redirected": 0,
            "dry_run": dry_run,
        }

    dupes = conn.execute("""
        SELECT name, entity_type, COUNT(*) as cnt
        FROM kg_entities
        GROUP BY name, entity_type
        HAVING cnt > 1
    """).fetchall()

    groups_found = len(dupes)
    entities_merged = 0
    edges_redirected = 0

    for name, etype, count in dupes:
        rows = conn.execute(
            "SELECT id, mentions FROM kg_entities "
            "WHERE name = ? AND entity_type = ? ORDER BY COALESCE(mentions, 0) DESC, id ASC",
            (name, etype),
        ).fetchall()

        if len(rows) < 2:
            continue

        keep_id = rows[0][0]
        delete_ids = [r[0] for r in rows[1:]]

        for did in delete_ids:
            result = merge_entities(conn, keep_id, did, dry_run=dry_run)
            edges_redirected += result["edges_redirected"]
            entities_merged += 1

    if not dry_run:
        conn.commit()

    return {
        "groups_found": groups_found,
        "entities_merged": entities_merged,
        "edges_redirected": edges_redirected,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Semantic dedup (new)
# ---------------------------------------------------------------------------


def compute_semantic_merge_candidates(
    conn: AnyConnection,
    threshold: float | None = None,
    max_pairs: int = 100,
) -> list[dict]:
    """Find entity pairs with similar names (cosine similarity > threshold).

    Uses the existing model2vec embedding model to encode entity names.
    Only compares entities within the same entity_type to avoid merging
    "Google Inc" (org) with "Google" (place).

    Args:
        conn: SQLite connection with kg_entities table.
        threshold: Cosine similarity threshold (0.0–1.0). Default from
            config ``kg_dedup_threshold`` (0.92).
        max_pairs: Maximum merge candidates to return (safety cap).

    Returns:
        List of dicts: [{"keep_id": N, "merge_id": N, "keep_name": str,
                         "merge_name": str, "similarity": float}, ...]
    """
    if threshold is None:
        threshold = get_config().kg_dedup_threshold if get_config is not None else 0.92
    try:
        from infra._lazy_imports import get_embedding_search

        es = get_embedding_search()
        if es.model is None:
            return []
        np = es.np
        if np is None:
            return []
    except Exception as e:
        logger.warning("compute_semantic_merge_candidates failed: %s", e)
        import logging

        logging.getLogger(__name__).debug(
            "semantic dedup: embedding model unavailable: %s", e
        )
        return []

    # Fetch all entities grouped by type
    entities = conn.execute(
        "SELECT id, name, entity_type FROM kg_entities ORDER BY entity_type, name"
    ).fetchall()
    if len(entities) < 2:
        return []

    # Group by entity_type
    by_type: dict[str, list[tuple[int, str]]] = {}
    for eid, name, etype in entities:
        by_type.setdefault(etype or "unknown", []).append((eid, name))

    candidates = []
    for etype, group in by_type.items():
        if len(group) < 2:
            continue
        ids = [g[0] for g in group]
        names = [g[1] for g in group]
        # Encode all names at once (batch is fast)
        vectors = es.encode(names)
        if vectors is None or len(vectors) != len(names):
            continue
        # Pairwise cosine similarity (dot product — vectors are L2-normalized)
        sim_matrix = np.dot(vectors, vectors.T)
        # Find pairs above threshold (upper triangle only)
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                sim = float(sim_matrix[i, j])
                if sim >= threshold:
                    # Keep the entity with higher mention count or lower id
                    keep_idx, merge_idx = i, j
                    keep_mentions = conn.execute(
                        "SELECT mentions FROM kg_entities WHERE id = ?",
                        (ids[keep_idx],),
                    ).fetchone()
                    merge_mentions = conn.execute(
                        "SELECT mentions FROM kg_entities WHERE id = ?",
                        (ids[merge_idx],),
                    ).fetchone()
                    if merge_mentions and keep_mentions:
                        if (merge_mentions[0] or 0) > (keep_mentions[0] or 0):
                            keep_idx, merge_idx = j, i
                    candidates.append(
                        {
                            "keep_id": ids[keep_idx],
                            "merge_id": ids[merge_idx],
                            "keep_name": names[keep_idx],
                            "merge_name": names[merge_idx],
                            "entity_type": etype,
                            "similarity": round(sim, 4),
                        }
                    )

    # Sort by similarity descending, cap at max_pairs
    candidates.sort(key=lambda x: float(x["similarity"]), reverse=True)  # type: ignore[arg-type]
    return candidates[:max_pairs]


def dedup_entities_semantic(
    conn: AnyConnection,
    threshold: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Run semantic entity deduplication using embedding similarity.

    Calls compute_semantic_merge_candidates() then merge_entities() for
    each pair above threshold.  Returns stats dict.
    """
    if threshold is None:
        threshold = get_config().kg_dedup_threshold if get_config is not None else 0.92
    candidates = compute_semantic_merge_candidates(conn, threshold=threshold)
    if not candidates:
        return {
            "semantic_groups_found": 0,
            "semantic_entities_merged": 0,
            "semantic_edges_redirected": 0,
            "dry_run": dry_run,
        }

    entities_merged = 0
    edges_redirected = 0
    merged_ids: set[int] = set()  # Prevent double-merge

    for c in candidates:
        keep_id = c["keep_id"]
        merge_id = c["merge_id"]
        # Skip if either was already merged in this pass
        if keep_id in merged_ids or merge_id in merged_ids:
            continue
        result = merge_entities(conn, keep_id, merge_id, dry_run=dry_run)
        edges_redirected += result["edges_redirected"]
        entities_merged += 1
        merged_ids.add(merge_id)

    if not dry_run:
        conn.commit()

    return {
        "semantic_groups_found": len(candidates),
        "semantic_entities_merged": entities_merged,
        "semantic_edges_redirected": edges_redirected,
        "dry_run": dry_run,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    semantic = "--semantic" in sys.argv
    default_threshold = (
        get_config().kg_dedup_threshold if get_config is not None else 0.92
    )
    threshold = default_threshold
    for arg in sys.argv[1:]:
        if arg.startswith("--threshold="):
            try:
                threshold = float(arg.split("=", 1)[1])
            except ValueError:
                pass
    env = os.environ.get("MEMORY_DB_PATH")
    db_path = (
        Path(env) if env is not None else resolve_active_memory_dir() / "memory.db"
    )
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            db_path = Path(arg)

    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)

    from infra.db_write_queue import sqlite_write_queue
    conn = sqlite_write_queue.start_session(db_path)
    try:
        stats = dedup_entities(conn, dry_run=dry_run)
        print(
            f"KG dedup (exact): {stats['groups_found']} duplicate groups, "
            f"{stats['entities_merged']} entities merged, "
            f"{stats['edges_redirected']} edges redirected"
            f"{' (dry run)' if dry_run else ''}"
        )

        if semantic:
            sem_stats = dedup_entities_semantic(
                conn, threshold=threshold, dry_run=dry_run
            )
            print(
                f"KG dedup (semantic, threshold={threshold}): "
                f"{sem_stats['semantic_groups_found']} candidates, "
                f"{sem_stats['semantic_entities_merged']} entities merged, "
                f"{sem_stats['semantic_edges_redirected']} edges redirected"
                f"{' (dry run)' if dry_run else ''}"
            )
            stats.update(sem_stats)
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    main()
