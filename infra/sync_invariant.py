"""Sync invariant checker — detects drift across memory subsystems.

Compares row counts between the memories table and its satellite tables
(FTS, embeddings, vector index, KG, chunks, tiers, metadata) to detect
when subsystems are out of sync. Also detects reverse ghost rows:
subsystem entries pointing to deleted memories.

Usage:
    from infra.sync_invariant import check_sync_invariant
    result = check_sync_invariant(conn)
    # result = {"overall": "healthy"|"drift"|"empty", "subsystems": {...}, "ghosts": {...}}
"""

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


def _count(conn: AnyConnection, sql: str) -> int:
    """Execute a COUNT query and return the result, or 0 on error."""
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row is not None else 0
    except Exception:
        return 0


def _check_subsystem(
    conn: AnyConnection, name: str, count: int, total: int, threshold: float = 0.80
) -> dict:
    """Classify a subsystem's health based on its count vs total.

    Returns {"name": ..., "count": N, "status": "healthy"|"drift"|"empty", "detail": ...}
    """
    if total == 0:
        return {
            "name": name,
            "count": count,
            "status": "empty",
            "detail": "no memories",
        }
    ratio = count / total
    if ratio >= threshold:
        return {
            "name": name,
            "count": count,
            "status": "healthy",
            "detail": f"{ratio:.0%} coverage",
        }
    elif count == 0:
        return {
            "name": name,
            "count": count,
            "status": "empty",
            "detail": "0 rows — not indexed",
        }
    else:
        return {
            "name": name,
            "count": count,
            "status": "drift",
            "detail": f"{ratio:.0%} coverage ({count}/{total})",
        }


def _detect_reverse_ghosts(conn: AnyConnection) -> dict:
    """Detect subsystem entries pointing to deleted or missing memories.

    Checks FTS, embeddings, KG facts, and chunks for orphaned entries.
    Returns {"fts": N, "embeddings": N, "kg_facts": N, "chunks": N, "total": N}.
    """
    ghosts = {}

    # FTS ghosts: entries in memories_fts for soft-deleted memories
    # FTS id column stores the memory id (TEXT), match against memories.id
    try:
        ghosts["fts"] = _count(
            conn,
            "SELECT COUNT(*) FROM memories_fts "
            "WHERE id IN (SELECT id FROM memories WHERE deleted_at IS NOT NULL)",
        )
    except Exception:
        ghosts["fts"] = 0

    # Embedding ghosts: embeddings for soft-deleted memories
    try:
        ghosts["embeddings"] = _count(
            conn,
            "SELECT COUNT(*) FROM memory_embeddings "
            "WHERE memory_id IN (SELECT id FROM memories WHERE deleted_at IS NOT NULL)",
        )
    except Exception:
        ghosts["embeddings"] = 0

    # KG fact ghosts: facts extracted from soft-deleted memories
    try:
        ghosts["kg_facts"] = _count(
            conn,
            "SELECT COUNT(*) FROM kg_facts "
            "WHERE source_memory IN (SELECT id FROM memories WHERE deleted_at IS NOT NULL)",
        )
    except Exception:
        ghosts["kg_facts"] = 0

    # Chunk ghosts: chunks for soft-deleted memories
    try:
        ghosts["chunks"] = _count(
            conn,
            "SELECT COUNT(*) FROM memory_chunks "
            "WHERE parent_id IN (SELECT id FROM memories WHERE deleted_at IS NOT NULL)",
        )
    except Exception:
        ghosts["chunks"] = 0

    ghosts["total"] = sum(ghosts.values())
    return ghosts


def check_sync_invariant(conn: AnyConnection) -> dict:
    """Check all subsystem row counts against the memories table.

    Returns:
        {
            "overall": "healthy"|"drift"|"empty",
            "total_memories": N,
            "subsystems": {
                "fts": {"count": N, "status": ..., "detail": ...},
                "embeddings": ...,
                "vector_index": ...,
                "kg_entities": ...,
                "kg_edges": ...,
                "kg_facts": ...,
                "chunks": ...,
                "tiers": ...,
                "metadata": ...,
                "adaptive_retention": ...,
            }
        }
    """
    total = _count(conn, "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
    if total == 0:
        return {"overall": "empty", "total_memories": 0, "subsystems": {}}

    # FTS index
    fts_count = _count(conn, "SELECT COUNT(*) FROM memories_fts")

    # Embeddings
    emb_count = _count(conn, "SELECT COUNT(*) FROM memory_embeddings")

    # Vector index — check n_vectors from metadata row
    vec_row = conn.execute("SELECT n_vectors FROM memory_vec_idx WHERE id=1").fetchone()
    vec_count = vec_row[0] if vec_row else 0

    # KG entities
    kg_ent = _count(conn, "SELECT COUNT(*) FROM kg_entities")

    # KG edges
    kg_edges = _count(conn, "SELECT COUNT(*) FROM kg_edges")

    # KG facts
    kg_facts = _count(conn, "SELECT COUNT(*) FROM kg_facts")

    # Chunks — detect notes that should be chunked but have zero chunks
    # A note with content > _QW5_CHUNK_THRESHOLD (2000) chars should have >= 1 chunk
    long_notes = _count(
        conn,
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NULL AND length(content) > 2000",
    )
    chunked_long = _count(
        conn,
        "SELECT COUNT(DISTINCT parent_id) FROM memory_chunks "
        "WHERE parent_id IN (SELECT id FROM memories WHERE deleted_at IS NULL AND length(content) > 2000)",
    )
    unchunked = max(0, long_notes - chunked_long)

    # Tiers (non-null tier on active notes)
    tiered = _count(
        conn,
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND tier IS NOT NULL",
    )

    # Metadata (non-null metadata on active notes)
    meta = _count(
        conn,
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND metadata IS NOT NULL",
    )

    # Adaptive retention (halflife computed)
    adaptive = _count(
        conn,
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NULL "
        "AND metadata IS NOT NULL "
        "AND json_extract(metadata, '$.adaptive_halflife_days') IS NOT NULL",
    )

    subsystems = {
        "fts": _check_subsystem(conn, "fts", fts_count, total),
        "embeddings": _check_subsystem(conn, "embeddings", emb_count, total),
        "vector_index": _check_subsystem(conn, "vector_index", vec_count, total),
        "kg_entities": _check_subsystem(
            conn, "kg_entities", kg_ent, total, threshold=0.05
        ),
        "kg_edges": _check_subsystem(conn, "kg_edges", kg_edges, total, threshold=0.01),
        "kg_facts": _check_subsystem(conn, "kg_facts", kg_facts, total, threshold=0.01),
        "chunks": {
            "name": "chunks",
            "count": chunked_long,
            "status": "drift"
            if unchunked > 0
            else ("empty" if long_notes == 0 else "healthy"),
            "detail": f"{unchunked} long notes missing chunks"
            if unchunked > 0
            else f"all {long_notes} long notes chunked",
        },
        "tiers": _check_subsystem(conn, "tiers", tiered, total),
        "metadata": _check_subsystem(conn, "metadata", meta, total),
        "adaptive_retention": _check_subsystem(
            conn, "adaptive_retention", adaptive, total
        ),
    }

    # Overall status: "drift" (partially indexed) is the real problem.
    # "empty" is expected for optional subsystems (KG, facts) on small DBs.
    statuses = [s["status"] for s in subsystems.values()]
    has_drift = any(s == "drift" for s in statuses)
    has_healthy = any(s == "healthy" for s in statuses)
    all_empty = all(s in ("empty", "healthy") for s in statuses)

    if has_drift:
        overall = "drift"
    elif all_empty and not has_healthy:
        overall = "empty"
    else:
        overall = "healthy"

    # Reverse ghost detection: subsystem entries pointing to deleted memories
    ghosts = _detect_reverse_ghosts(conn)

    return {
        "overall": overall,
        "total_memories": total,
        "subsystems": subsystems,
        "ghosts": ghosts,
    }


def get_drifted_subsystems(result: dict) -> list[str]:
    """Return names of subsystems that are in drift state (partially indexed)."""
    return [
        name
        for name, info in result.get("subsystems", {}).items()
        if info["status"] == "drift"
    ]


def format_sync_report(result: dict) -> str:
    """Format a human-readable sync report."""
    lines = []
    lines.append(f"Overall: {result['overall'].upper()}")
    lines.append(f"Total memories: {result['total_memories']}")
    lines.append("")
    for name, info in result.get("subsystems", {}).items():
        status_icon = {"healthy": "OK", "drift": "!!", "empty": "XX"}.get(
            info["status"], "??"
        )
        lines.append(
            f"  [{status_icon}] {name:20s} {info['count']:>8d}  {info['detail']}"
        )

    ghosts = result.get("ghosts", {})
    if ghosts.get("total", 0) > 0:
        lines.append("")
        lines.append("Reverse ghosts (orphaned subsystem entries):")
        for name in ["fts", "embeddings", "kg_facts", "chunks"]:
            count = ghosts.get(name, 0)
            if count > 0:
                lines.append(f"  [!!] {name:20s} {count:>8d}  orphaned entries")
    return "\n".join(lines)
