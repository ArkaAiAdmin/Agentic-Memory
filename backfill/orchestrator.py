"""Universal backfill orchestrator — one script, all indexes.

Unifies rebuild_index, backfill_chunks, fact_extraction, rebuild_vec_index,
kg_dedup, and backfill_orphans into a single entry point with three modes:

  health  — Report which indexes are stale/missing (no writes).
  incremental — Rebuild only stale/missing indexes (default).
  full    — Drop and rebuild everything from markdown source.

Auto-trigger: called from save_pipeline after N saves (gated by
MEMORY_BACKFILL_INTERVAL, default 0 = off).

Usage:
    venv/bin/python backfill_all.py              # incremental
    venv/bin/python backfill_all.py --full        # full rebuild
    venv/bin/python backfill_all.py --health      # health check only
    venv/bin/python backfill_all.py --auto        # auto-trigger mode
"""

from __future__ import annotations

import logging

import os
import sqlite3
import sys
import threading
from typing import Any
from pathlib import Path

__all__ = [
    "backfill_all",
    "health_check",
    "backfill_incremental",
    "backfill_full",
    "auto_backfill",
]

logger = logging.getLogger(__name__)

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from infra.memory_common import (
    safe_close_db,
    connection_pool,
)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

_TABLES = {
    "memories": "SELECT COUNT(*) FROM memories",
    "memory_embeddings": "SELECT COUNT(*) FROM memory_embeddings",
    "memories_fts": "SELECT COUNT(*) FROM memories_fts",
    "memory_chunks": "SELECT COUNT(*) FROM memory_chunks",
    "memory_chunks_fts": "SELECT COUNT(*) FROM memory_chunks_fts",
    "kg_facts": "SELECT COUNT(*) FROM kg_facts",
    "kg_entities": "SELECT COUNT(*) FROM kg_entities",
    "kg_edges": "SELECT COUNT(*) FROM kg_edges",
    "memory_vec_idx": "SELECT COUNT(*) FROM memory_vec_idx",
    "memory_vec_keys": "SELECT COUNT(*) FROM memory_vec_keys",
    "backlinks": "SELECT COUNT(*) FROM backlinks",
}


# ---------------------------------------------------------------------------
# Per-index backfill primitives
#
# Extracted to backfill.index_backfills (2026-06-20). Re-exported here so
# existing callers using ``from backfill_all import _backfill_fts`` etc.
# keep working without modification.
# ---------------------------------------------------------------------------
from backfill.index_backfills import (  # noqa: E402, F401
    _backfill_memories_from_markdown,
    _backfill_fts,
    _backfill_embeddings,
    _backfill_chunks,
    _backfill_chunks_fts,
    _backfill_backlinks,
    _backfill_vec_index_raw,
    _backfill_crdt_vectors,
    _backfill_tiers,
    _backfill_shared_memories,
)

# ---------------------------------------------------------------------------
# Knowledge-graph backfill primitives
#
# Extracted to backfill.kg_backfills (2026-06-20). Re-exported here so
# existing callers using ``from backfill_all import _backfill_kg_facts``
# etc. keep working without modification.
# ---------------------------------------------------------------------------
from backfill.kg_backfills import (  # noqa: E402, F401
    _is_stopword,
    _is_valid_entity,
    _backfill_kg_facts,
    _backfill_kg_graph,
    _ENTITY_STOPWORDS,
)


def health_check(db_path: str | Path | None = None) -> dict:
    """Return per-table row counts + staleness flags.

    Returns::

        {
            "db_path": "...",
            "tables": {
                "memories": {"count": 121, "ok": True, "reason": "populated"},
                "kg_entities": {"count": 0, "ok": False, "reason": "empty — run --full or --incremental"},
                ...
            },
            "all_healthy": True/False,
            "stale_count": N,
        }
    """
    db_path = _resolve_db(db_path)
    result: dict[str, Any] = {
        "db_path": str(db_path),
        "tables": {},
        "all_healthy": True,
        "stale_count": 0,
    }

    if not db_path.exists():
        for table in _TABLES:
            result["tables"][table] = {
                "count": 0,
                "ok": False,
                "reason": "DB does not exist",
            }
        result["all_healthy"] = False
        result["stale_count"] = len(_TABLES)
        return result

    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Get existing tables
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # Get memories count and active memories count to check for index drift
        memories_count = 0
        active_count = 0
        if "memories" in existing:
            try:
                memories_count = conn.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0]
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(memories)").fetchall()
                }
                if "deleted_at" in cols:
                    active_count = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
                    ).fetchone()[0]
                else:
                    active_count = memories_count
            except Exception:
                logger.warning(
                    "Failed to query active memory count during integrity check"
                )
                pass

        for table, sql in _TABLES.items():
            if table not in existing:
                result["tables"][table] = {
                    "count": 0,
                    "ok": False,
                    "reason": "table missing",
                }
                result["all_healthy"] = False
                result["stale_count"] += 1
                continue
            try:
                count = conn.execute(sql).fetchone()[0]
                if count == 0:
                    ok = False
                    reason = "empty — needs backfill"
                elif (
                    table == "memory_vec_keys"
                    and active_count > 0
                    and count != active_count
                ):
                    ok = False
                    reason = (
                        f"mismatch: {count} keys vs {active_count} active memories"
                    )
                elif (
                    table == "memory_embeddings"
                    and active_count > 0
                    and count != active_count
                ):
                    ok = False
                    reason = f"mismatch: {count} embeddings vs {active_count} active memories"
                elif (
                    table == "memories_fts"
                    and active_count > 0
                    and count != active_count
                ):
                    ok = False
                    reason = (
                        f"mismatch: {count} fts rows vs {active_count} active memories"
                    )
                else:
                    ok = True
                    reason = "populated"

                if not ok:
                    result["all_healthy"] = False
                    result["stale_count"] += 1
                result["tables"][table] = {"count": count, "ok": ok, "reason": reason}
            except Exception as e:
                logger.warning("health_check failed: %s", e)
                result["tables"][table] = {
                    "count": 0,
                    "ok": False,
                    "reason": f"error: {e}",
                }
                result["all_healthy"] = False
                result["stale_count"] += 1
    finally:
        safe_close_db(conn)

    return result


# ---------------------------------------------------------------------------
# Incremental backfill — rebuild only stale indexes
# ---------------------------------------------------------------------------


def backfill_incremental(
    db_path: str | Path | None = None,
    source_dir: str | Path | None = None,
    commit_every: int = 50,
    progress_every: int = 100,
) -> dict:
    """Rebuild only stale/missing indexes. Returns stats dict.

    2026-06-19 fix: same per-batch commit / progress-marker pattern
    as ``backfill_full`` (see comments there for the data-loss story).
    """
    db_path = _resolve_db(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"db_path does not exist: {db_path}. "
            f"Pass a real path, or omit db_path to use the default. "
            f"For modes, use --incremental / --full, not a bare arg."
        )
    source_dir = _resolve_source(source_dir, db_path)
    h = health_check(db_path)
    stats: dict[str, Any] = {
        "db_path": str(db_path),
        "operations": [],
        "total_stale": h["stale_count"],
    }

    if h["all_healthy"]:
        stats["result"] = "all_healthy"
        return stats

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        # 1. memories table — always ensure populated
        mem_count = h["tables"]["memories"]["count"]
        if mem_count == 0:
            _backfill_memories_from_markdown(conn, source_dir, db_path)
            stats["operations"].append({"op": "memories", "result": "rebuilt"})
        else:
            stats["operations"].append(
                {"op": "memories", "result": "ok", "count": mem_count}
            )

        # 2. memories_fts
        if not h["tables"]["memories_fts"]["ok"]:
            _backfill_fts(conn)
            stats["operations"].append({"op": "memories_fts", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "memories_fts", "result": "ok"})

        # 3. memory_embeddings
        if not h["tables"]["memory_embeddings"]["ok"]:
            _backfill_embeddings(conn)
            stats["operations"].append({"op": "memory_embeddings", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "memory_embeddings", "result": "ok"})

        # 4. memory_chunks
        if not h["tables"]["memory_chunks"]["ok"]:
            _backfill_chunks(conn)
            stats["operations"].append({"op": "memory_chunks", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "memory_chunks", "result": "ok"})

        # 5. memory_chunks_fts
        if not h["tables"]["memory_chunks_fts"]["ok"]:
            _backfill_chunks_fts(conn)
            stats["operations"].append({"op": "memory_chunks_fts", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "memory_chunks_fts", "result": "ok"})

        # 6. kg_facts (needs MEMORY_KNOWLEDGE_GRAPH=1)
        if not h["tables"]["kg_facts"]["ok"]:
            n_facts = _backfill_kg_facts(
                conn, commit_every=commit_every, progress_every=progress_every
            )
            stats["operations"].append(
                {"op": "kg_facts", "result": "rebuilt", "count": n_facts}
            )
        else:
            stats["operations"].append({"op": "kg_facts", "result": "ok"})

        # 7. kg_entities + kg_edges (derived from kg_facts)
        if not h["tables"]["kg_entities"]["ok"] or not h["tables"]["kg_edges"]["ok"]:
            _backfill_kg_graph(conn)
            stats["operations"].append({"op": "kg_graph", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "kg_graph", "result": "ok"})

        # 8. backlinks
        if not h["tables"]["backlinks"]["ok"]:
            _backfill_backlinks(conn)
            stats["operations"].append({"op": "backlinks", "result": "rebuilt"})
        else:
            stats["operations"].append({"op": "backlinks", "result": "ok"})

        # 9. CRDT version vectors
        crdt_stats = _backfill_crdt_vectors(conn)
        stats["operations"].append({"op": "crdt_vectors", **crdt_stats})

        # 10. Temporal tiers
        tier_stats = _backfill_tiers(conn)
        stats["operations"].append({"op": "tiers", **tier_stats})

        conn.commit()
    finally:
        safe_close_db(conn)

    # 9. Vector index (must run outside pool connection to avoid lock conflicts)
    if (
        not h["tables"]["memory_vec_idx"]["ok"]
        or not h["tables"]["memory_vec_keys"]["ok"]
    ):
        _backfill_vec_index_raw(db_path)
        stats["operations"].append({"op": "vec_index", "result": "rebuilt"})
    else:
        stats["operations"].append({"op": "vec_index", "result": "ok"})

    stats["result"] = "completed"
    rebuilt = [op for op in stats["operations"] if op["result"] == "rebuilt"]
    stats["rebuilt_count"] = len(rebuilt)
    _run_post_backfill_cleanup(db_path, stats)
    return stats


# ---------------------------------------------------------------------------
# Full backfill — drop and rebuild everything
# ---------------------------------------------------------------------------


def backfill_full(
    db_path: str | Path | None = None,
    source_dir: str | Path | None = None,
    commit_every: int = 50,
    progress_every: int = 100,
) -> dict:
    """Full rebuild from markdown source. Drops and recreates all indexes.

    2026-06-19 fix: stop wiping kg_* tables at the start. The previous
    behavior cleared the live KG before the new extraction ran, which
    meant a crash mid-run lost all existing data. We now use UPSERT
    semantics via ``index_facts_for_memory`` (which is idempotent on
    re-run), so a partial run leaves a valid partial result.

    Two new params: ``commit_every`` (per-batch commit cadence) and
    ``progress_every`` (log-line cadence). These are also exposed as
    CLI flags --commit-every and --progress-every in main().
    """
    db_path = _resolve_db(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"db_path does not exist: {db_path}. "
            f"Pass a real path, or omit db_path to use the default. "
            f"For modes, use --incremental / --full, not a bare arg."
        )
    source_dir = _resolve_source(source_dir, db_path)

    # DATA-LOSS WARNING (2026-06-22): A full rebuild from markdown
    # source only recovers what's in the markdown frontmatter + body.
    # The following data lives ONLY in SQLite and WILL BE LOST:
    #   - CRDT version vectors & logical clocks (memory_field_crdt)
    #   - Wiki backlinks & KG edges (kg_edges, kg_facts, kg_entities)
    #   - User access logs (user_access_log)
    #   - Concept drift metrics (concept_drift, drift_alarms)
    #   - ARC ghosts & stats (arc_ghosts, arc_stats)
    #   - Task queue (task_queue), sync log (sync_log)
    #   - CTR feedback, review schedule, auto-save status
    #
    # Restored from sidecars: shared_memories is persisted as
    # `*.shared.json` alongside each markdown note and restored
    # during rebuild via _backfill_shared_memories.
    logger.warning(
        "FULL REBUILD: relational metadata tables (CRDT vectors, "
        "KG edges, access logs, drift metrics, ARC state, task queue, "
        "shared pool, sync log) will be LOST. "
        "Re-run cron scripts to repopulate after rebuild completes."
    )

    stats: dict[str, Any] = {
        "db_path": str(db_path),
        "operations": [],
        "mode": "full",
    }

    # 2026-06-19 fix: only clear NON-KG tables. The KG tables
    # (kg_facts, kg_edges, kg_entities, kg_entities_fts) are now
    # built via UPSERT (index_facts_for_memory is idempotent) so
    # a partial run leaves a valid state. Wiping them at the start
    # was the bug that caused silent data loss on 2026-06-19.
    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        with conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "memory_chunks" in tables:
                conn.execute("DELETE FROM memory_chunks")
            if "memory_chunks_fts" in tables:
                try:
                    conn.execute("DELETE FROM memory_chunks_fts")
                except sqlite3.OperationalError:
                    pass
    except Exception as e:
        logger.warning("Failed to clear derived tables before full rebuild: %s", e)
    finally:
        safe_close_db(conn)

    # Delegate to rebuild_index.py for full rebuild (it handles schema + memories + FTS5 + embeddings)
    try:
        from rebuild_index import rebuild_index

        rebuild_index(str(source_dir), str(db_path))
        stats["operations"].append({"op": "rebuild_index", "result": "completed"})
    except Exception as e:
        logger.error("rebuild_index failed: %s", e)
        stats["operations"].append({"op": "rebuild_index", "result": f"failed: {e}"})

    # Then backfill remaining indexes
    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        _backfill_chunks(conn)
        stats["operations"].append({"op": "memory_chunks", "result": "rebuilt"})

        _backfill_chunks_fts(conn)
        stats["operations"].append({"op": "memory_chunks_fts", "result": "rebuilt"})

        # 2026-06-19: pass commit_every / progress_every so the
        # 2-3 hour LLM run is killable and observable.
        n_facts = _backfill_kg_facts(
            conn, commit_every=commit_every, progress_every=progress_every
        )
        stats["operations"].append(
            {"op": "kg_facts", "result": "rebuilt", "count": n_facts}
        )

        _backfill_kg_graph(conn)
        stats["operations"].append({"op": "kg_graph", "result": "rebuilt"})

        _backfill_backlinks(conn)
        stats["operations"].append({"op": "backlinks", "result": "rebuilt"})

        # CRDT version vectors and temporal tiers
        crdt_stats = _backfill_crdt_vectors(conn)
        stats["operations"].append({"op": "crdt_vectors", **crdt_stats})
        tier_stats = _backfill_tiers(conn)
        stats["operations"].append({"op": "tiers", **tier_stats})

        # 11. shared memories — restore from sidecar JSON files so they
        # survive full rebuilds (the canonical durable fix, see
        # backfill_all.py DATA-LOSS WARNING block).
        shared_stats = _backfill_shared_memories(conn, source_dir)
        stats["operations"].append({"op": "shared_memories", **shared_stats})

        conn.commit()
    finally:
        safe_close_db(conn)

    # Vector index (runs outside pool connection)
    _backfill_vec_index_raw(db_path)
    stats["operations"].append({"op": "vec_index", "result": "rebuilt"})

    stats["result"] = "completed"
    _run_post_backfill_cleanup(db_path, stats)
    return stats


# ---------------------------------------------------------------------------
# Auto-trigger — called from save_pipeline after N saves
# ---------------------------------------------------------------------------

_save_counter = 0
_save_counter_lock = threading.Lock()


def auto_backfill(db_path: str | Path | None = None) -> dict | None:
    """Increment save counter; trigger incremental backfill at interval.

    Gated by MEMORY_BACKFILL_INTERVAL (default 0 = off).
    Returns stats dict if triggered, None if skipped.
    """
    global _save_counter
    interval = int(os.environ.get("MEMORY_BACKFILL_INTERVAL", "0"))
    if interval <= 0:
        return None

    with _save_counter_lock:
        _save_counter += 1
        if _save_counter < interval:
            return None
        _save_counter = 0

    logger.info("Auto-backfill triggered (every %d saves)", interval)
    return backfill_incremental(db_path, commit_every=50, progress_every=100)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------


def backfill_all(
    mode: str = "incremental",
    db_path: str | Path | None = None,
    source_dir: str | Path | None = None,
    commit_every: int = 50,
    progress_every: int = 100,
) -> dict:
    """Universal backfill entry point.

    Args:
        mode: "health", "incremental", or "full"
        db_path: Path to memory.db (auto-detected if None)
        source_dir: Path to memory/ directory (auto-detected if None)
        commit_every: 2026-06-19 — commit every N memories during the
            slow LLM extraction phase, so a kill/crash doesn't lose
            the last 50 memories' worth of progress.
        progress_every: 2026-06-19 — log a progress line every N
            memories so the user can see ETA and abort if stuck.

    Returns:
        Stats dict with per-index results.
    """
    if mode == "health":
        return health_check(db_path)
    elif mode == "full":
        return backfill_full(
            db_path,
            source_dir,
            commit_every=commit_every,
            progress_every=progress_every,
        )
    else:
        return backfill_incremental(
            db_path,
            source_dir,
            commit_every=commit_every,
            progress_every=progress_every,
        )


# ---------------------------------------------------------------------------
# Internal backfill helpers
# ---------------------------------------------------------------------------


def _resolve_db(db_path: str | Path | None) -> Path:
    if db_path is not None:
        return Path(db_path).resolve()
    env_db = os.environ.get("MEMORY_DB_PATH")
    if env_db:
        return Path(env_db).resolve()
    from infra.infrastructure import resolve_active_memory_dir
    return resolve_active_memory_dir() / "memory.db"


_resolve_db_path = _resolve_db


def _resolve_source(source_dir: str | Path | None, db_path: Path) -> Path:
    if source_dir is not None:
        return Path(source_dir).resolve()
    # memory/ directory is sibling to memory.db
    mem_dir = db_path.parent
    if mem_dir.name == "memory":
        return mem_dir
    return mem_dir / "memory"


def _run_post_backfill_cleanup(db_path: Path, stats: dict) -> None:
    """Run orphaned notes cleanup and knowledge graph deduplication."""
    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        try:
            from backfill.backfill_orphans import cleanup as _cleanup_orphans

            orphans_stats = _cleanup_orphans(conn)
            stats["operations"].append(
                {"op": "clean_orphans", "result": "completed", "details": orphans_stats}
            )
        except Exception as e:
            logger.warning("Failed to run orphaned notes cleanup: %s", e)

        if os.environ.get("MEMORY_KNOWLEDGE_GRAPH") == "1":
            try:
                from kg.kg_dedup import (
                    dedup_entities as _dedup_entities,
                    dedup_entities_semantic as _dedup_entities_semantic,
                )

                exact_stats = _dedup_entities(conn, dry_run=False)
                semantic_stats = _dedup_entities_semantic(conn, dry_run=False)
                stats["operations"].append(
                    {
                        "op": "kg_dedup",
                        "result": "completed",
                        "details": {"exact": exact_stats, "semantic": semantic_stats},
                    }
                )
            except Exception as e:
                logger.warning("Failed to run KG deduplication: %s", e)
    finally:
        safe_close_db(conn)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    mode = "incremental"
    db_path = None
    source_dir = None
    commit_every = 50
    progress_every = 100

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--full":
            mode = "full"
        elif args[i] == "--health":
            mode = "health"
        elif args[i] == "--incremental":
            mode = "incremental"
        elif args[i] == "--auto":
            result = auto_backfill(db_path)
            if result is None:
                print("Auto-backfill not triggered (MEMORY_BACKFILL_INTERVAL=0)")
                return 0
            _print_result(result)
            return 0
        elif args[i] == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 1
        elif args[i] == "--source" and i + 1 < len(args):
            source_dir = args[i + 1]
            i += 1
        elif args[i] == "--commit-every" and i + 1 < len(args):
            try:
                commit_every = int(args[i + 1])
            except ValueError:
                print(
                    f"warning: --commit-every expected int, got {args[i + 1]!r}; using default {commit_every}",
                    file=sys.stderr,
                )
            i += 1
        elif args[i] == "--progress-every" and i + 1 < len(args):
            try:
                progress_every = int(args[i + 1])
            except ValueError:
                print(
                    f"warning: --progress-every expected int, got {args[i + 1]!r}; using default {progress_every}",
                    file=sys.stderr,
                )
            i += 1
        elif args[i] == "--llm-max-tokens" and i + 1 < len(args):
            try:
                # 2026-06-19: plumb to env so llm_extraction.py picks it up
                # without an import cycle. Default 256 (was 1024).
                # 2026-06-22 (C5 fix): warn if the operator has already
                # set this via TOML or env var so they can see which
                # source won. The CLI value still wins (we overwrite
                # here), but at least it's visible.
                if "MEMORY_LLM_EXTRACTION_MAX_TOKENS" in os.environ and os.environ[
                    "MEMORY_LLM_EXTRACTION_MAX_TOKENS"
                ] != str(int(args[i + 1])):
                    print(
                        f"warning: --llm-max-tokens={args[i + 1]} overrides "
                        f"MEMORY_LLM_EXTRACTION_MAX_TOKENS="
                        f"{os.environ['MEMORY_LLM_EXTRACTION_MAX_TOKENS']!r} "
                        f"from the env (which itself may have come from "
                        f"memory.toml [llm_extraction].max_tokens).",
                        file=sys.stderr,
                    )
                os.environ["MEMORY_LLM_EXTRACTION_MAX_TOKENS"] = str(int(args[i + 1]))
            except ValueError:
                print(
                    f"warning: --llm-max-tokens expected int, got {args[i + 1]!r}; using config default",
                    file=sys.stderr,
                )
            i += 1
        elif args[i] == "--llm-hybrid-threshold" and i + 1 < len(args):
            try:
                # 2026-06-19 P3.3: plumb to env
                # 2026-06-22 (C5 fix): same warning policy as
                # --llm-max-tokens — flag dual-plumbing so the operator
                # knows the CLI value won.
                if "MEMORY_LLM_HYBRID_THRESHOLD" in os.environ and os.environ[
                    "MEMORY_LLM_HYBRID_THRESHOLD"
                ] != str(float(args[i + 1])):
                    print(
                        f"warning: --llm-hybrid-threshold={args[i + 1]} overrides "
                        f"MEMORY_LLM_HYBRID_THRESHOLD="
                        f"{os.environ['MEMORY_LLM_HYBRID_THRESHOLD']!r} "
                        f"from the env.",
                        file=sys.stderr,
                    )
                os.environ["MEMORY_LLM_HYBRID_THRESHOLD"] = str(float(args[i + 1]))
            except ValueError:
                print(
                    f"warning: --llm-hybrid-threshold expected float, got {args[i + 1]!r}; using config default",
                    file=sys.stderr,
                )
            i += 1
        elif args[i] == "--llm-force":
            # 2026-06-19 P3.3: force LLM on every memory
            os.environ["MEMORY_LLM_FORCE"] = "1"
        elif args[i] == "--no-llm-hybrid":
            # 2026-06-19 P3.3: disable hybrid, never use LLM
            os.environ["MEMORY_LLM_HYBRID"] = "0"
        elif args[i] in ("incremental", "full", "health"):
            # Bare mode name (legacy form). Accept with deprecation
            # warning so callers don't silently create a 22 MB garbage
            # DB at the repo root by mistake.
            if mode == "incremental":
                mode = args[i]
            print(
                f"warning: bare '{args[i]}' is deprecated; use --{args[i]}",
                file=sys.stderr,
            )
        elif not args[i].startswith("-"):
            # positional: treat as db_path for backward compat
            db_path = args[i]
        i += 1

    # Rule 7 guard: bare invocation (no explicit mode flag and no db_path)
    # used to default to `incremental` and silently create a 22 MB garbage
    # DB at the repo root. Refuse to run so an operator can't blow away data
    # by accident. A mode flag (--incremental/--full/--health/--auto) or a
    # positional db_path is required.
    _explicit_mode = any(
        a in ("--incremental", "--full", "--health", "--auto") for a in args
    )
    if not _explicit_mode and not db_path:
        print(
            "error: backfill_all.py requires an explicit mode or db_path.\n"
            "  Usage:\n"
            "    venv/bin/python backfill_all.py --incremental\n"
            "    venv/bin/python backfill_all.py --full\n"
            "    venv/bin/python backfill_all.py --db /path/to/memory.db\n"
            "  Bare invocation is rejected (would create a garbage DB at repo root).",
            file=sys.stderr,
        )
        return 2

    result = backfill_all(
        mode,
        db_path,
        source_dir,
        commit_every=commit_every,
        progress_every=progress_every,
    )
    _print_result(result)
    return 0


def _print_result(result: dict):
    """Pretty-print backfill results."""
    if "all_healthy" in result:
        # Health check output
        print(f"\n=== Health Check: {result['db_path']} ===")
        for table, info in result["tables"].items():
            status = "✓" if info["ok"] else "✗"
            print(f"  {status} {table}: {info['count']} rows — {info['reason']}")
        print(f"\n  All healthy: {result['all_healthy']}")
        print(f"  Stale indexes: {result['stale_count']}")
    else:
        # Backfill output
        print(f"\n=== Backfill ({result.get('mode', 'incremental')}) ===")
        for op in result["operations"]:
            status = "✓" if op["result"] in ("ok", "completed", "rebuilt") else "✗"
            extra = f" ({op.get('count', '')})" if "count" in op else ""
            print(f"  {status} {op['op']}: {op['result']}{extra}")
        print(f"\n  Result: {result.get('result', 'unknown')}")
        if "rebuilt_count" in result:
            print(f"  Rebuilt: {result['rebuilt_count']} indexes")


if __name__ == "__main__":
    sys.exit(main())
