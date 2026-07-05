"""Self-Directed Memory Management for agentic-memory.

Automatically evaluates, archives, and manages memory tiers based on
access patterns, importance, and age. Opt-in via MEMORY_SELF_DIRECTED=1.

Features:
  - Heartbeat: periodic re-evaluation of all notes
  - Auto-importance scoring: compute importance from access + success
  - Auto-archival: move low-importance notes to cold tier
  - Tier management: hot/warm/cold assignment
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


__all__ = [
    "SELF_DIRECTED_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "compute_importance",
    "run_heartbeat",
    "archive_low_importance",
    "tier_stats",
]

# SELF_DIRECTED_ENABLED is dynamically resolved via __getattr__
SELF_DIRECTED_ENABLED: bool  # PEP 526 annotation for LSP; runtime value comes from __getattr__

# ---------------------------------------------------------------------------
# Importance Scoring
# ---------------------------------------------------------------------------

# Weights for importance calculation
_W_ACCESS = 0.3  # access count weight
_W_SUCCESS = 0.3  # success score weight
_W_RECENCY = 0.25  # recency weight
_W_PINNED = 0.15  # pinned bonus

# Tier thresholds
_TIER_HOT_THRESHOLD = 0.7
_TIER_WARM_THRESHOLD = 0.3
# Below warm = cold

# Auto-archive threshold
_ARCHIVE_THRESHOLD = 0.15
_ARCHIVE_MIN_AGE_DAYS = 90


def _parse_ts_to_epoch(ts, fallback: float) -> float:
    if ts is None:
        return fallback
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return float(ts)
        except ValueError:
            pass
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
    return fallback


def compute_importance(conn: AnyConnection, memory_id: str) -> float:
    """Compute importance score for a memory (0.0 to 1.0).

    Factors: access count, success score, recency (with adaptive halflife), pinned status.
    If MEMORY_ADAPTIVE_RETENTION=1, reads the note's adaptive_halflife_days from
    metadata to compute a more accurate recency score.
    """
    # Check if metadata column exists (might not in test DBs)
    has_metadata = True
    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        has_metadata = "metadata" in cols
    except Exception:
        has_metadata = False

    if has_metadata:
        row = conn.execute(
            "SELECT access_count, success_score, updated_at, pinned, created_at, metadata "
            "FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT access_count, success_score, updated_at, pinned, created_at "
            "FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
    if not row:
        return 0.0

    access_count = row[0] or 0
    success_score = row[1] or 0.0
    updated_at = row[2]
    pinned = row[3]
    created_at = row[4]
    metadata_json = row[5] if has_metadata and len(row) > 5 else None

    now = time.time()

    # Access score: log-scaled, capped at 10
    access_score = min(1.0, math.log1p(access_count) / math.log1p(10))

    # Success score: already 0-1
    success_norm = max(0.0, min(1.0, success_score))

    # Recency score: exponential decay using adaptive half-life if available
    ts = updated_at or created_at
    epoch = _parse_ts_to_epoch(ts, now - 365 * 86400)
    age_days = max(0.0, (now - epoch) / 86400)

    # Try to get adaptive half-life from metadata
    half_life = 180.0  # default
    if metadata_json:
        try:
            meta = json.loads(metadata_json or "{}")
            adaptive_hl = meta.get("adaptive_halflife_days")
            if adaptive_hl is not None:
                half_life = float(adaptive_hl)
        except (json.JSONDecodeError, TypeError):
            pass

    # Recency: 50% at half_life days
    recency_score = math.exp(-0.693 * age_days / half_life)

    # Pinned bonus
    pinned_score = 1.0 if pinned else 0.0

    importance = (
        _W_ACCESS * access_score
        + _W_SUCCESS * success_norm
        + _W_RECENCY * recency_score
        + _W_PINNED * pinned_score
    )

    return max(0.0, min(1.0, importance))


def _assign_tier(importance: float) -> str:
    """Assign tier based on importance score."""
    if importance >= _TIER_HOT_THRESHOLD:
        return "hot"
    elif importance >= _TIER_WARM_THRESHOLD:
        return "warm"
    else:
        return "cold"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def _backfill_drifted_subsystems(conn: AnyConnection, drifted: list[str]) -> dict:
    """Targeted backfill for specific drifted subsystems.

    Only re-indexes notes that are missing from the drifted subsystems,
    rather than rebuilding everything from scratch.
    """
    import time as _time

    start = _time.time()
    stats: dict[str, Any] = {"drifted": drifted, "fixed": {}, "elapsed": 0.0}

    # Fetch all active note IDs and contents
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
    note_ids = [r[0] for r in rows]
    note_contents = {r[0]: r[1] for r in rows}
    total = len(note_ids)

    # Backfill FTS
    if "fts" in drifted:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO memories_fts(rowid, id, content, tags, category) "
                "SELECT rowid, id, content, tags, category FROM memories WHERE deleted_at IS NULL"
            )
            conn.commit()
            stats["fixed"]["fts"] = total
        except Exception as e:
            stats["fixed"]["fts"] = f"error: {e}"

    # Backfill embeddings
    if "embeddings" in drifted:
        try:
            from infra._lazy_imports import get_embedding_search

            es = get_embedding_search()
            es.wait_for_model(timeout_s=30.0)
            count = 0
            for nid, content in note_contents.items():
                if content:
                    es.index_embedding(
                        conn, nid, content, category="", tags=[], source_file=""
                    )
                    count += 1
            stats["fixed"]["embeddings"] = count
        except Exception as e:
            stats["fixed"]["embeddings"] = f"error: {e}"

    # Backfill KG entities + edges
    if any(s in drifted for s in ("kg_entities", "kg_edges")):
        try:
            from knowledge_graph import (
                KG_ENABLED,
                ensure_kg_schema,
                index_kg_for_memory,
            )

            if KG_ENABLED:
                ensure_kg_schema(conn)
                count = 0
                for nid, content in note_contents.items():
                    if content:
                        index_kg_for_memory(conn, nid, content)
                        count += 1
                stats["fixed"]["kg_entities"] = count
                stats["fixed"]["kg_edges"] = count
        except Exception as e:
            stats["fixed"]["kg_entities"] = f"error: {e}"

    # Backfill KG facts (index_facts_for_memory_bulk, NOT
    # index_facts_for_memory). The bulk variant is regex-only by
    # design — it never loads the 3B LLM or runs per-memory
    # inference. Using the per-save variant here would deadlock
    # the loky worker pool and freeze the machine for hours on
    # databases with thousands of high-importance memories. LLM
    # extraction still runs at save time for individual notes.
    # (2026-06-26 fix; refactored 2026-06-26 to use the dedicated
    # bulk function instead of a force_regex flag.)
    if "kg_facts" in drifted:
        try:
            from fact import (
                ensure_facts_schema,
                index_facts_for_memory_bulk,
            )

            ensure_facts_schema(conn)
            count = 0
            for nid, content in note_contents.items():
                if content:
                    result = index_facts_for_memory_bulk(conn, nid, content)
                    count += result.get("facts", 0)
            stats["fixed"]["kg_facts"] = count
        except Exception as e:
            stats["fixed"]["kg_facts"] = f"error: {e}"

    # Backfill chunks
    if "chunks" in drifted:
        try:
            from search_pipeline import _qw5_index_chunks_for

            count = 0
            for nid, content in note_contents.items():
                if content:
                    _qw5_index_chunks_for(conn, nid, content)
                    count += 1
            stats["fixed"]["chunks"] = count
        except Exception as e:
            stats["fixed"]["chunks"] = f"error: {e}"

    # Backfill tiers
    if "tiers" in drifted:
        try:
            count = 0
            for nid in note_ids:
                conn.execute(
                    "UPDATE memories SET tier = CASE WHEN importance_score >= 4 THEN 'hot' WHEN importance_score >= 2 THEN 'warm' ELSE 'cold' END WHERE id = ? AND tier IS NULL",
                    (nid,),
                )
                count += 1
            conn.commit()
            stats["fixed"]["tiers"] = count
        except Exception as e:
            stats["fixed"]["tiers"] = f"error: {e}"

    # Backfill metadata
    if "metadata" in drifted:
        try:
            count = 0
            for nid in note_ids:
                conn.execute(
                    "UPDATE memories SET metadata = '{}' "
                    "WHERE id = ? AND metadata IS NULL",
                    (nid,),
                )
                count += 1
            conn.commit()
            stats["fixed"]["metadata"] = count
        except Exception as e:
            stats["fixed"]["metadata"] = f"error: {e}"

    # Rebuild vector index if drifted
    if "vector_index" in drifted:
        try:
            import subprocess
            from pathlib import Path as _Path

            venv_python = sys.executable
            rebuild_script = str(_Path(__file__).parent / "rebuild_vec_index.py")
            db_path_row = conn.execute("PRAGMA database_list").fetchone()
            db_path_str = str(db_path_row[2]) if db_path_row is not None else ""
            subprocess.run(
                [venv_python, rebuild_script, db_path_str],
                capture_output=True,
                timeout=300,
            )
            stats["fixed"]["vector_index"] = "rebuilt"
        except Exception as e:
            stats["fixed"]["vector_index"] = f"error: {e}"

    # Backfill adaptive retention
    if "adaptive_retention" in drifted:
        try:
            from adaptive_retention import batch_update_retention

            result = batch_update_retention()
            stats["fixed"]["adaptive_retention"] = result.get("updated", 0)
        except Exception as e:
            stats["fixed"]["adaptive_retention"] = f"error: {e}"

    # Backfill backlinks
    if "backlinks" in drifted:
        try:
            from save.indexers import _index_backlinks

            count = 0
            for nid, content in note_contents.items():
                if content:
                    _index_backlinks(conn, nid, content)
                    count += 1
            conn.commit()
            stats["fixed"]["backlinks"] = count
        except Exception as e:
            stats["fixed"]["backlinks"] = f"error: {e}"

    stats["elapsed"] = round(_time.time() - start, 2)
    return stats


def _cleanup_orphaned_subsystem_data(
    conn: AnyConnection, dry_run: bool = False
) -> dict:
    """Remove subsystem entries that reference notes no longer in memories.

    Cleans orphaned:
        - memory_chunks (parent_id not in memories)
        - memory_embeddings (memory_id not in memories)
        - memory_vec_keys (memory_id not in memories)
        - kg_facts (source_memory not in memories)
        - kg_edges (source_id or target_id references orphaned kg_entities)
        - kg_entities (no remaining edges or facts)

    Returns stats dict with counts of cleaned orphans per subsystem.
    """
    import time as _time

    start = _time.time()
    stats: dict = {}

    # 1. Orphaned chunks
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM memory_chunks c
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = c.parent_id)
        """).fetchone()
        orphaned_chunks = rows[0] if rows else 0
        if orphaned_chunks > 0 and not dry_run:
            conn.execute("""
                DELETE FROM memory_chunks WHERE parent_id NOT IN
                (SELECT id FROM memories)
            """)
        stats["orphaned_chunks"] = orphaned_chunks
    except sqlite3.OperationalError:
        stats["orphaned_chunks"] = "no table"

    # 2. Orphaned embeddings
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM memory_embeddings e
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = e.memory_id)
        """).fetchone()
        orphaned_embeddings = rows[0] if rows else 0
        if orphaned_embeddings > 0 and not dry_run:
            conn.execute("""
                DELETE FROM memory_embeddings WHERE memory_id NOT IN
                (SELECT id FROM memories)
            """)
        stats["orphaned_embeddings"] = orphaned_embeddings
    except sqlite3.OperationalError:
        stats["orphaned_embeddings"] = "no table"

    # 3. Orphaned vec keys
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM memory_vec_keys v
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = v.memory_id)
        """).fetchone()
        orphaned_vec_keys = rows[0] if rows else 0
        if orphaned_vec_keys > 0 and not dry_run:
            conn.execute("""
                DELETE FROM memory_vec_keys WHERE memory_id NOT IN
                (SELECT id FROM memories)
            """)
        stats["orphaned_vec_keys"] = orphaned_vec_keys
    except sqlite3.OperationalError:
        stats["orphaned_vec_keys"] = "no table"

    # 4. Orphaned KG facts
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM kg_facts f
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = f.source_memory)
        """).fetchone()
        orphaned_facts = rows[0] if rows else 0
        if orphaned_facts > 0 and not dry_run:
            conn.execute("""
                DELETE FROM kg_facts WHERE source_memory NOT IN
                (SELECT id FROM memories)
            """)
        stats["orphaned_kg_facts"] = orphaned_facts
    except sqlite3.OperationalError:
        stats["orphaned_kg_facts"] = "no table"

    # 5. Orphaned KG edges (edges referencing entities with no other edges/facts)
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM kg_edges e
            WHERE NOT EXISTS (SELECT 1 FROM kg_entities ent WHERE ent.id = e.source_id)
               OR NOT EXISTS (SELECT 1 FROM kg_entities ent WHERE ent.id = e.target_id)
        """).fetchone()
        orphaned_edges = rows[0] if rows else 0
        if orphaned_edges > 0 and not dry_run:
            conn.execute("""
                DELETE FROM kg_edges WHERE source_id NOT IN (SELECT id FROM kg_entities)
                   OR target_id NOT IN (SELECT id FROM kg_entities)
            """)
        stats["orphaned_kg_edges"] = orphaned_edges
    except sqlite3.OperationalError:
        stats["orphaned_kg_edges"] = "no table"

    # 6. Orphaned KG entities (no remaining edges or facts)
    try:
        rows = conn.execute("""
            SELECT COUNT(*) FROM kg_entities e
            WHERE NOT EXISTS (SELECT 1 FROM kg_edges e2 WHERE e2.source_id = e.id OR e2.target_id = e.id)
              AND NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name)
        """).fetchone()
        orphaned_entities = rows[0] if rows else 0
        if orphaned_entities > 0 and not dry_run:
            conn.execute("""
                DELETE FROM kg_entities WHERE id IN (
                    SELECT e.id FROM kg_entities e
                    WHERE NOT EXISTS (SELECT 1 FROM kg_edges e2 WHERE e2.source_id = e.id OR e2.target_id = e.id)
                      AND NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name)
                )
            """)
        stats["orphaned_kg_entities"] = orphaned_entities
    except sqlite3.OperationalError:
        stats["orphaned_kg_entities"] = "no table"

    if not dry_run:
        conn.commit()

    total_orphans = sum(v for v in stats.values() if isinstance(v, int))
    stats["total_cleaned"] = total_orphans
    stats["elapsed"] = round(_time.time() - start, 2)
    return stats


def run_heartbeat(
    conn: AnyConnection, dry_run: bool = False, db_path: str | None = None
) -> dict:
    """Re-evaluate all memories: compute importance, assign tier, archive.

    Tier migration is BIDIRECTIONAL: notes can be promoted (cold→warm,
    warm→hot) as well as demoted (hot→warm, warm→cold) based on their
    current importance score.

    If MEMORY_ADAPTIVE_RETENTION=1, batch-computes adaptive half-lives
    before importance scoring so recency decay is access-aware.

    If subsystem drift is detected, auto-backfills the missing subsystems
    before proceeding.

    Returns stats: {"evaluated": N, "tier_changes": N, "promoted": N,
                    "archived": N, "sync_backfill": {...} or None}
    """
    # Step 0: check subsystem sync, backfill if drifted
    sync_backfill = None
    try:
        from infra.sync_invariant import check_sync_invariant, get_drifted_subsystems

        drift_result = check_sync_invariant(conn)
        drifted = get_drifted_subsystems(drift_result)
        if drifted:
            sync_backfill = _backfill_drifted_subsystems(conn, drifted)
    except Exception:
        pass

    # Step 0.5: clean orphaned subsystem data (entries referencing deleted notes)
    orphan_cleanup = None
    try:
        orphan_cleanup = _cleanup_orphaned_subsystem_data(conn, dry_run=dry_run)
    except Exception:
        pass

    # Step 1: batch-compute adaptive half-lives if enabled
    adaptive_stats = None
    try:
        from adaptive_retention import (
            batch_update_retention,
            ADAPTIVE_RETENTION_ENABLED,
        )

        if ADAPTIVE_RETENTION_ENABLED:
            adaptive_stats = batch_update_retention(dry_run=dry_run, db_path=db_path, conn=conn)
    except ImportError:
        pass

    now = time.time()
    rows = conn.execute(
        "SELECT id, tier, pinned, updated_at, created_at FROM memories "
        "WHERE deleted_at IS NULL"
    ).fetchall()

    evaluated = 0
    tier_changes = 0
    promoted = 0
    archived = 0

    # Tier order for promotion detection
    _TIER_ORDER = {"cold": 0, "warm": 1, "hot": 2}

    for memory_id, current_tier, pinned, updated_at, created_at in rows:
        importance = compute_importance(conn, memory_id)
        new_tier = _assign_tier(importance)

        # Pinned notes are never cold — ensure at least warm
        if pinned and new_tier == "cold":
            new_tier = "warm"

        if new_tier != current_tier:
            tier_changes += 1
            # Detect promotion (cold→warm, cold→hot, warm→hot)
            if _TIER_ORDER.get(new_tier, 0) > _TIER_ORDER.get(current_tier, 0):
                promoted += 1
            if not dry_run:
                conn.execute(
                    "UPDATE memories SET tier = ?, importance_score = ? WHERE id = ?",
                    (new_tier, importance, memory_id),
                )

        # Auto-archive: cold + low importance + old enough
        if new_tier == "cold" and importance < _ARCHIVE_THRESHOLD and not pinned:
            ts_val = created_at or updated_at
            epoch = _parse_ts_to_epoch(ts_val, now)
            age_days = max(0.0, (now - epoch) / 86400)
            if age_days >= _ARCHIVE_MIN_AGE_DAYS:
                archived += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET tier = 'cold', importance_score = ? WHERE id = ?",
                        (importance, memory_id),
                    )

        evaluated += 1

    if not dry_run:
        conn.commit()

    return {
        "evaluated": evaluated,
        "tier_changes": tier_changes,
        "promoted": promoted,
        "archived": archived,
        "adaptive_retention": adaptive_stats,
        "sync_backfill": sync_backfill,
        "orphan_cleanup": orphan_cleanup,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def archive_low_importance(
    conn: AnyConnection,
    threshold: float = _ARCHIVE_THRESHOLD,
    min_age_days: int = _ARCHIVE_MIN_AGE_DAYS,
    dry_run: bool = False,
) -> dict:
    """Move low-importance, old notes to cold tier.

    Returns stats: {"archived": N, "skipped": N}
    """
    now = time.time()
    cutoff_ts = now - (min_age_days * 86400)

    rows = conn.execute(
        "SELECT id, importance_score, pinned, created_at, updated_at "
        "FROM memories WHERE deleted_at IS NULL AND tier != 'cold'"
    ).fetchall()

    archived = 0
    skipped = 0

    for memory_id, importance, pinned, created_at, updated_at in rows:
        if pinned:
            skipped += 1
            continue
        imp = (
            importance
            if importance is not None
            else compute_importance(conn, memory_id)
        )
        if imp >= threshold:
            skipped += 1
            continue
        ts = created_at or updated_at
        epoch = _parse_ts_to_epoch(ts, now)
        if epoch >= cutoff_ts:
            skipped += 1
            continue

        archived += 1
        if not dry_run:
            conn.execute(
                "UPDATE memories SET tier = 'cold', importance_score = ? WHERE id = ?",
                (imp, memory_id),
            )

    if not dry_run:
        conn.commit()

    return {"archived": archived, "skipped": skipped, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Tier Stats
# ---------------------------------------------------------------------------


def tier_stats(conn: AnyConnection) -> dict:
    """Return tier distribution and importance statistics."""
    try:
        tiers = {}
        for row in conn.execute(
            "SELECT tier, COUNT(*), AVG(importance_score) "
            "FROM memories WHERE deleted_at IS NULL GROUP BY tier"
        ).fetchall():
            tiers[row[0]] = {"count": row[1], "avg_importance": round(row[2] or 0, 4)}

        total_row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()
        total_val = int(total_row[0]) if total_row is not None else 0

        pinned_row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND pinned = 1"
        ).fetchone()
        pinned_val = int(pinned_row[0]) if pinned_row is not None else 0

        return {
            "total": total_val,
            "pinned": pinned_val,
            "tiers": tiers,
        }
    except sqlite3.OperationalError:
        return {"total": 0, "pinned": 0, "tiers": {}}


def tier_stats_db(db_path: str | Path) -> dict:
    """tier_stats with connection lifecycle managed."""
    from infra.db import open_db
    with open_db(Path(db_path), timeout=10.0, pooled=True, write=False) as conn:
        return tier_stats(conn)


from infra.memory_common import make_lazy_getattr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


__getattr__ = make_lazy_getattr({"SELF_DIRECTED_ENABLED": "self_directed"})
