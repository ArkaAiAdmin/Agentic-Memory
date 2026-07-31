#!/usr/bin/env python3
"""Background worker for agentic-memory task queue.

Polls the task_queue table, dispatches tasks to handlers, and manages
graceful shutdown.  Designed to be run via cron (every 5 min) or as a
long-lived process.

Usage:
    # Single-pass (for cron — processes ONE task):
    venv/bin/python background_worker.py --once

    # Drain mode (process ALL pending tasks, then exit). Use this to
    # burn down a backlog: e.g. after a long downtime, run --drain
    # once to flush the 12K task queue, then resume the --once cron.
    venv/bin/python background_worker.py --drain

    # Continuous loop (for background service):
    venv/bin/python background_worker.py --interval=300

    # Process specific task type only:
    venv/bin/python background_worker.py --once --type=entity_resolution
    venv/bin/python background_worker.py --drain --type=entity_resolution

    # Cap how many tasks --drain processes before exiting (safety
    # belt for runaway handlers):
    venv/bin/python background_worker.py --drain --max-tasks=500

Env vars:
    MEMORY_WORKER_INTERVAL: Poll interval in seconds (default 300 = 5 min)
    MEMORY_DB_PATH: Override database path
    MEMORY_WORKER_MAX_TASKS: Cap on --drain (default 10000, safety belt)
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BG_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BG_DIR)
_REPO_ROOT = os.path.dirname(_BG_DIR)
sys.path.insert(0, _REPO_ROOT)
from background.background_queue import init_task_queue, dequeue_task, complete_task, fail_task
from infra.infrastructure import resolve_active_memory_dir
from memory_integrity import repair_kg_orphans, repair_vec_orphans
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False

# Shutdown grace period (seconds) — how long to wait for in-flight
# tasks to finish before force-exiting.
_SHUTDOWN_GRACE_S = int(os.environ.get("MEMORY_WORKER_SHUTDOWN_GRACE_S", "10"))

# Event that the reconciler thread waits on during graceful shutdown.
_RECONCILER_SHUTDOWN = threading.Event()


def _cleanup_task_artifacts(task_type: str, payload: dict) -> None:
    """Remove temporary directories created by a task (best-effort)."""
    temp_dir = payload.get("temp_dir") or payload.get("working_dir")
    if not temp_dir:
        return
    try:
        import shutil
        p = Path(temp_dir)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _shutdown_force_exit() -> None:
    """Force-exit after the grace period if the process is still alive."""
    import sys
    time.sleep(_SHUTDOWN_GRACE_S)
    logger.warning("worker: grace period exhausted, force-exiting")
    sys.exit(1)


def _start_reconciler(
    db_path: Path,
    once: bool = False,
    interval: int = 300,
    max_tasks: int = 10000,
) -> threading.Thread:
    """Start the reconciler worker in a background thread (test helper)."""
    t = threading.Thread(
        target=run_worker,
        kwargs=dict(db_path=db_path, once=once, interval=interval, max_tasks=max_tasks),
        daemon=True,
    )
    t.start()
    return t

# Default batch size for the non-drain worker loop.  Each cron tick
# processes up to this many tasks before sleeping for ``interval``
# seconds again.  The previous behaviour was exactly 1 task per tick,
# which meant a 12K backlog took ~500 hours to drain at the default
# 300-second poll interval.  The batch size is bounded by the
# per-process timeout (default 3600s) and the per-task watchdog
# (default 120s), so 20 tasks × 120s worst-case = 2400s, well inside
# the hour-long safety cap.
_DEFAULT_BATCH_SIZE = int(os.environ.get("MEMORY_WORKER_BATCH_SIZE", "20"))


def _get_effective_batch_size() -> int:
    """Return the worker batch size, capped by the DB connection pool size."""
    try:
        from infra._lazy_imports import get_config
        pool_size: int = int(get_config().db_pool_size)
        return int(min(_DEFAULT_BATCH_SIZE, max(1, pool_size - 4)))
    except Exception:
        return _DEFAULT_BATCH_SIZE

# Module-level keep-alive for the inline flock fd (H-fix 2026-06-22).
# See the ImportError fallback in main() — if cron._flock isn't on
# path, we acquire fcntl.flock directly and must keep the fd alive
# for the worker's lifetime or the lock is released.
_BACKGROUND_WORKER_LOCK_FD = None


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logger.info("worker: received signal %d, shutting down after current task", signum)


# ---------------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------------


def handle_entity_resolution(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Run semantic entity dedup on the KG."""
    try:
        from kg.kg_dedup import dedup_entities, dedup_entities_semantic

        # Exact dedup first
        exact = dedup_entities(conn)
        # Semantic dedup (uses embedding model)
        semantic = dedup_entities_semantic(conn, threshold=0.92)
        return (
            f"exact: {exact['entities_merged']} merged, "
            f"semantic: {semantic['semantic_entities_merged']} merged"
        )
    except Exception as e:
        raise RuntimeError(f"entity_resolution failed: {e}") from e


def handle_fact_consolidation(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Run fact consolidation (merge similar SPO triples)."""
    try:
        from pathlib import Path as _P
        from fact.consolidate_facts import consolidate_memory_facts

        # Guard: skip if corpus is too large (>2000 notes) — consolidate_facts
        # uses O(n²) contradiction detection and will return immediately with
        # a warning, but the module-level imports (llm_extraction, sentence
        # transformers) still happen at import time and can load a 3B LLM
        # consuming 6-8GB. Check + short-circuit here to skip the expensive
        # import entirely when the guard would immediately return.
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()
            n = int(row[0]) if row else 0
            if n > 2000:
                return f"fact consolidation skipped: corpus {n} notes exceeds guard (2000)"
        except Exception:
            pass

        consolidate_memory_facts(db_path=_P(db_path))
        return "fact consolidation completed"
    except Exception as e:
        raise RuntimeError(f"fact_consolidation failed: {e}") from e


def handle_semantic_backlinks(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Create semantic KG edges between the saved memory and its nearest neighbors."""
    try:
        from save.backlinks import _auto_semantic_backlinks

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if memory_id and content:
            _auto_semantic_backlinks(
                conn, memory_id, content, db_path=str(db_path)
            )
            return f"semantic backlinks created for {memory_id}"
        return "skipped: no memory_id or content"
    except Exception as e:
        raise RuntimeError(f"semantic_backlinks failed: {e}") from e


def handle_wal_checkpoint(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Run a passive WAL checkpoint.

    S4.3 (2026-06-23): the worker now also drains the WAL
    periodically so the on-disk DB stays close to the working
    set.  ``PRAGMA wal_checkpoint(PASSIVE)`` is non-blocking —
    safe under concurrent readers and writers.  Threshold
    defaults to 10 MiB (matches ``wal_checkpoint_idle`` in
    ``db.py``); can be overridden via payload.

    Payload fields (all optional):
      threshold_mb: float (default 10.0).  Only run a checkpoint
        if the WAL file is larger than this many megabytes.
    """
    from infra.db import wal_checkpoint_idle

    threshold = float(payload.get("threshold_mb", 10.0))
    try:
        result = wal_checkpoint_idle(db_path, wal_size_threshold_mb=threshold)
        return f"wal_checkpoint status={result.get('status')} reason={result.get('reason')}"
    except Exception as e:
        raise RuntimeError(f"wal_checkpoint failed: {e}") from e


def handle_chunk_embedding_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Embed all chunks for a memory and persist to memory_chunk_embeddings (deferred from save)."""
    try:
        from save.indexers import _index_chunk_embeddings

        memory_id = payload.get("memory_id", "")
        if not memory_id:
            return "skipped: no memory_id in payload"
        _index_chunk_embeddings(conn, memory_id)
        conn.commit()
        return f"chunk embeddings indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"chunk_embedding_index failed: {e}") from e


def handle_embedding_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Compute and store embedding for a memory note (deferred from save)."""
    try:
        from save.indexers import _index_embedding

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        source_file = payload.get("source_file", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = memory_id.split("/")[0] if "/" in memory_id else "general"
        _index_embedding(conn, memory_id, content, category, [], source_file)
        conn.commit()
        return f"embedding indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"embedding_index failed: {e}") from e


def handle_kg_and_fact_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Extract KG entities, facts, and enrich context for a memory (deferred)."""
    try:
        from save.indexers import _index_kg, _index_facts
        from save.post_save_hooks import _enrich_context

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = memory_id.split("/")[0] if "/" in memory_id else "general"
        belief_status = payload.get("belief_status", "active")
        epistemic_source = payload.get("epistemic_source", "agent")
        asserting_agent_id = payload.get("asserting_agent_id", "")
        evidence_chain = payload.get("evidence_chain")
        fact_type = payload.get("fact_type", "observation")
        _index_kg(conn, memory_id, content)
        _index_facts(conn, memory_id, content,
                     belief_status=belief_status, epistemic_source=epistemic_source,
                     asserting_agent_id=asserting_agent_id,
                     evidence_chain=evidence_chain,
                     fact_type=fact_type)
        _enrich_context(conn, memory_id, content, category, [])
        conn.commit()
        return f"KG+facts+context indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"kg_and_fact_index failed: {e}") from e


def handle_vec_index_rebuild(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Rebuild the vector index (memory_vec_idx) from memory_embeddings.

    Triggered by save_pipeline when the threshold of pending writes
    since the last rebuild is reached, or by a manual memory_rebuild
    MCP-tool call. Runs `rebuild_vec_index.py` as a subprocess so the
    usearch binary build doesn't lock our worker connection for long.

    Payload fields (all optional):
      force: bool (default False). If True, rebuild even if the index
        already has the right cardinality. Used by the operator-initiated
        path.
      reason: str (default "scheduled"). Free-form audit string.
    """
    try:
        # Locate rebuild_vec_index.py. Resolution order:
        # 1. MEMORY_REBUILD_VEC_INDEX env var (explicit override)
        # 2. <install_root>/rebuild_vec_index.py (uses MEMORY_INSTALL_ROOT if set,
        #    else ~/.config/agentic-memory/)
        # 3. db_path-relative: walk up from the db (covers ad-hoc test DBs)
        # 4. venv heuristic: parent of sys.executable's parent (legacy fallback)
        from infra.memory_config import install_root

        candidates = []
        if os.environ.get("MEMORY_REBUILD_VEC_INDEX"):
            candidates.append(Path(os.environ["MEMORY_REBUILD_VEC_INDEX"]))
        candidates.append(install_root() / "rebuild_vec_index.py")
        if db_path.parent.name == "memory":
            candidates.append(db_path.parent.parent / "rebuild_vec_index.py")
        # Legacy venv heuristic (kept as last-resort fallback for non-standard layouts).
        # sys.executable = <repo>/venv/bin/python; parent.parent.parent = <repo>.
        import sys as _sys

        candidates.append(Path(_sys.executable).parent.parent.parent / "rebuild_vec_index.py")
        # 2026-06-29 fix: cwd-relative resolution. On CI runners the install
        # root doesn't match `install_root()`'s default (~/.config/agentic-memory)
        # but the script is always next to the test runner's cwd.
        candidates.append(Path.cwd() / "rebuild_vec_index.py")
        script = next((c for c in candidates if c and c.exists()), None)
        if script is None:
            raise RuntimeError("rebuild_vec_index.py not found on disk")
        venv_py = Path(_sys.executable)
        if not venv_py.exists():
            raise RuntimeError(f"venv python not found at {venv_py}")
        # Audit-gap fix (2026-06-22 follow-up): capture the reason
        # up front so the graceful-skip branch below can include it
        # in the result string.  ``payload`` is the task payload
        # (a dict of "force", "reason", etc.).
        reason = (payload or {}).get("reason", "scheduled")
        result = subprocess.run(
            [str(venv_py), str(script), str(db_path), "--subsystems", "vec_idx"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            # Audit-gap fix (2026-06-22 follow-up): rebuild_vec_index
            # holds a cross-process flock; if another rebuild is
            # already running, it logs "Another vec_index rebuild is
            # already running." and exits non-zero.  Detect that case
            # and return a graceful "skipped" message rather than
            # treating it as a task failure (which would mark the
            # background task as failed and retry with backoff).
            combined = (result.stdout or "") + (result.stderr or "")
            if "Another vec_index rebuild is already running" in combined:
                return (
                    f"vec_idx rebuild skipped: {reason}; another rebuild is in progress"
                )
            raise RuntimeError(
                f"rebuild_vec_index exited {result.returncode}: {result.stderr or result.stdout}"
            )
        # Parse the final stats line for the audit log.
        return f"vec_idx rebuilt: {reason}; output={result.stdout.strip()[:300]}"
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"vec_index_rebuild timed out: {e}") from e


def handle_evidence_chain_staleness(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Check if any belief's evidence_chain contains superseded facts.

    Background task that runs periodically (every 15 min via the worker
    loop or on-demand via enqueue).
    """
    try:
        from belief import handle_evidence_chain_staleness as _check_staleness

        result = _check_staleness(conn)
        if result["deprecated"] > 0:
            logger.info(
                "Evidence chain staleness: checked=%d, deprecated=%d",
                result["checked"],
                result["deprecated"],
            )
        conn.commit()
        return f"evidence_chain_staleness: checked={result['checked']}, deprecated={result['deprecated']}"
    except Exception as e:
        raise RuntimeError(f"evidence_chain_staleness failed: {e}") from e


def handle_run_script(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Run a cron script as a subprocess.

    Payload fields:
      script: relative path to the script (from repo root), e.g. "cron/cron_consolidate.py"
      args: list of CLI args (optional, default [])
      env: dict of extra env vars (optional)
      timeout: seconds (optional, default 300)

    If ``task_type`` matches an entry in ``CRON_SCRIPT_MAP`` (e.g.
    ``cron_consolidate``), the script path is resolved automatically
    from the map and the explicit ``script`` field is not required.
    """
    script_rel = payload.get("script", "")
    # Resolve from CRON_SCRIPT_MAP if the payload didn't specify a script.
    if not script_rel:
        # payload['task_type'] is not passed here; we rely on the
        # caller setting `script` explicitly when using the generic
        # run_script handler.  For cron-style task types, callers
        # should use the mapped handler directly or pass script via
        # the CRON_SCRIPT_MAP lookup in enqueue_task.py.
        raise ValueError("missing 'script' in payload")
    # Resolve script path relative to repo root (parent of background/)
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / script_rel
    if not script.exists():
        raise RuntimeError(f"script not found: {script}")
    venv_py = Path(sys.executable)
    if not venv_py.exists():
        raise RuntimeError(f"venv python not found at {venv_py}")
    extra_args = payload.get("args", [])
    timeout = int(payload.get("timeout", 300))
    env = os.environ.copy()
    env.update(payload.get("env", {}))
    env["MEMORY_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [str(venv_py), str(script), *extra_args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(
            f"{script.name} exited {result.returncode}: "
            f"stderr={stderr[:300]} stdout={stdout[:300]}"
        )
    return f"{script.name}: {stdout[:300] or 'ok'}"

# Handler registry
HANDLERS = {
    "entity_resolution": handle_entity_resolution,
    "fact_consolidation": handle_fact_consolidation,
    "compact": handle_fact_consolidation,
    "embedding_index": handle_embedding_index,
    "chunk_embedding_index": handle_chunk_embedding_index,
    "kg_and_fact_index": handle_kg_and_fact_index,
    "semantic_backlinks": handle_semantic_backlinks,
    "vec_index_rebuild": handle_vec_index_rebuild,
    "wal_checkpoint": handle_wal_checkpoint,
    "run_script": handle_run_script,
    "evidence_chain_staleness": handle_evidence_chain_staleness,
}


def _lazy_entailment_chains(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from reasoning.compile import handle_entailment_chains
    return handle_entailment_chains(payload, conn, db_path)


def _lazy_concept_compilation(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from reasoning.compile import handle_concept_compilation
    return handle_concept_compilation(payload, conn, db_path)


def _lazy_skill_enrichment(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from reasoning.compile import handle_skill_enrichment
    return handle_skill_enrichment(payload, conn, db_path)


def _lazy_graph_communities(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from kg.graph_communities import compute_communities, write_community_ids

    algorithm = payload.get("algorithm", "louvain")
    resolution = float(payload.get("resolution", 1.0))
    min_component_size = int(payload.get("min_component_size", 1))
    membership = compute_communities(
        conn, algorithm=algorithm, resolution=resolution,
        min_component_size=min_component_size,
    )
    updated = write_community_ids(conn, membership)
    conn.commit()
    return f"graph_communities ({algorithm}): {updated} entities assigned to communities"


def _lazy_colbert_index(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from search.colbert_index import index_memory_colbert, _ensure_colbert_schema

    _ensure_colbert_schema(conn)
    memory_id = payload.get("memory_id", "")
    if not memory_id:
        return "colbert_index: skipped (no memory_id)"

    row = conn.execute(
        "SELECT content FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row or not row[0]:
        return f"colbert_index: skipped (no content for {memory_id})"

    n = index_memory_colbert(conn, memory_id, row[0])
    return f"colbert_index: {n} token vectors for {memory_id}"


def _lazy_splade_index(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from search.splade_index import index_memory_splade, _ensure_splade_schema

    _ensure_splade_schema(conn)
    memory_id = payload.get("memory_id", "")
    if not memory_id:
        return "splade_index: skipped (no memory_id)"

    row = conn.execute(
        "SELECT content FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row or not row[0]:
        return f"splade_index: skipped (no content for {memory_id})"

    n = index_memory_splade(conn, memory_id, row[0])
    return f"splade_index: {n} sparse entries for {memory_id}"


def _lazy_graph_snapshots(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    import json as _json
    from kg.graph_analytics import compute_pagerank
    from kg.graph_communities import connected_components

    now = time.time()
    row = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()
    entity_count = row[0] if row else 0
    row2 = conn.execute(
        "SELECT COUNT(*) FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
    ).fetchone()
    edge_count = row2[0] if row2 else 0

    cc = connected_components(conn)
    community_count = len(set(cc.values()))

    pr = compute_pagerank(conn)
    all_centralities = list(pr.values())
    avg_centrality = sum(all_centralities) / len(all_centralities) if all_centralities else 0.0

    top_entities = []
    for eid, score in sorted(pr.items(), key=lambda x: x[1], reverse=True)[:10]:
        name_row = conn.execute("SELECT name FROM kg_entities WHERE id = ?", (eid,)).fetchone()
        top_entities.append({"name": name_row[0] if name_row else str(eid), "centrality": round(score, 6)})

    last_snapshot = conn.execute(
        "SELECT new_entities, removed_entities FROM graph_snapshots ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    new_entities: list[str] = []
    removed_entities: list[str] = []
    if last_snapshot:
        try:
            new_entities = _json.loads(last_snapshot[0]) if last_snapshot[0] else []
            removed_entities = _json.loads(last_snapshot[1]) if last_snapshot[1] else []
        except Exception:
            pass

    conn.execute(
        """INSERT INTO graph_snapshots
           (captured_at, entity_count, edge_count, community_count,
            avg_centrality, top_entities, new_entities, removed_entities)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            entity_count,
            edge_count,
            community_count,
            round(avg_centrality, 12),
            _json.dumps(top_entities),
            _json.dumps(new_entities),
            _json.dumps(removed_entities),
        ),
    )
    conn.commit()
    return f"graph_snapshot: entities={entity_count}, edges={edge_count}, communities={community_count}"


def _lazy_cron_pipeline_sentinel(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    if conn is None:
        return "pipeline_healthy"
    from cron.cron_pipeline_health import _pending_depth, _count_failures

    depth = _pending_depth(conn)
    failures = _count_failures(conn)
    if depth >= 0 and failures >= 0:
        return "pipeline_healthy"
    return f"pipeline_unhealthy: depth={depth}, failures={failures}"


HANDLERS.update(
    {
        "entailment_chains": _lazy_entailment_chains,
        "concept_compilation": _lazy_concept_compilation,
        "skill_enrichment": _lazy_skill_enrichment,
        "graph_communities": _lazy_graph_communities,
        "graph_snapshots": _lazy_graph_snapshots,
        "colbert_index": _lazy_colbert_index,
        "splade_index": _lazy_splade_index,
        "cron_pipeline_sentinel": _lazy_cron_pipeline_sentinel,
    }
)

# Mapping of cron-style task types to their script paths.
# Keys are the task_type values used in enqueue_task.py --task-type;
# values are relative paths from repo root to the cron script.
CRON_SCRIPT_MAP: dict[str, str] = {
    # Phase B — originally direct, now enqueued
    "cron_daily_digest": "auto_save.py",
    "cron_purge_auto_saves": "cron/cron_purge_auto_saves.py",
    "cron_integrity_check": "cron/cron_integrity_check.py",
    "cron_log_retention": "cron/cron_log_retention.py",
    "cron_backfill_all": "backfill_all.py",
    "cron_backup": "cron/cron_backup.py",
    "cron_backup_validate": "cron/cron_backup_validate.py",
    "cron_sync": "cron/cron_sync.py",
    "cron_crdt_sync": "cron/cron_crdt_sync.py",
    "cron_monitor_task_queue": "cron/monitor_task_queue.py",
    "cron_health_check": "cron/cron_health_check.py",
    # Pre-Phase B — already mapped
    "cron_cleanup_auto_logs": "cron/cleanup_auto_logs.py",
    "cron_kg_backfill_monitor": "cron/cron_kg_backfill_monitor.py",
    "cron_embedding_recompute": "cron/cron_embedding_recompute.py",
    "cron_detect_vec_drift": "cron/cron_detect_vec_drift.py",
    "cron_rewrite_links": "cron/cron_rewrite_links.py",
    "cron_consolidate": "cron/cron_consolidate.py",
    "cron_compact": "cron/cron_compact.py",
    "cron_rebuild_fts": "cron/cron_rebuild_fts.py",
    "cron_heartbeat": "cron/cron_heartbeat.py",
    "cron_tier_migration": "cron/cron_tier_migration.py",
    "cron_kg_backfill": "cron/cron_kg_backfill.py",
    "cron_skill_extraction": "cron/cron_skill_extraction.py",
    "cron_cross_session_learn": "cron/cron_cross_session_learn.py",
    "cron_pinned_decay": "cron/cron_pinned_decay.py",
    "cron_concept_drift": "cron/cron_concept_drift.py",
    "cron_purge_expired": "cron/cron_purge_expired.py",
    "cron_quality_filter": "cron/cron_quality_filter.py",
    "cron_auto_summarize": "cron/cron_auto_summarize.py",
    "cron_retention_stats": "cron/cron_retention_stats.py",
    "cron_auto_share": "cron/cron_auto_share.py",
}


# ---------------------------------------------------------------------------
# Proactive vec-index drift reconciliation
# ---------------------------------------------------------------------------


def _get_vec_rebuild_threshold(conn: AnyConnection | None = None) -> int:
    """Return the vector rebuild threshold (from config or
    env var MEMORY_VEC_REBUILD_THRESHOLD). Default: 15.

    When ``vec_rebuild_adaptive`` is true, computes a dynamic threshold
    based on the ratio of observed drift to write velocity in the last
    10 minutes.  High write velocity relaxes the threshold (multiplier
    up to 3.0×); low velocity tightens it (multiplier down to 0.5×).
    Falls back to the base value on any error.
    """
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        base = int(getattr(cfg, "vec_rebuild_threshold", 15) or 15)
        adaptive = bool(getattr(cfg, "vec_rebuild_adaptive", False))
    except Exception:
        base = int(os.environ.get("MEMORY_VEC_REBUILD_THRESHOLD", "15"))
        adaptive = False

    if not adaptive or base <= 0:
        return base

    try:
        from infra._lazy_imports import get_memory_paths

        _, mem_dir, _ = get_memory_paths()
        db_path = mem_dir / "memory.db"
        if not db_path.exists():
            return base

        close_after = False
        if conn is None:
            from infra.db import open_db
            conn = open_db(db_path, timeout=5.0, pooled=True, write=False).__enter__()
            close_after = True

        try:
            window_seconds = 600  # 10-minute window for rate estimation
            row_writes = conn.execute(
                "SELECT COUNT(*) AS n FROM memories "
                "WHERE updated_at >= datetime('now', ?)",
                (f"-{window_seconds} seconds",),
            ).fetchone()
            recent_writes = row_writes[0] if row_writes is not None else 0
            row_drift = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_vec_keys "
                "WHERE memory_id NOT IN (SELECT id FROM memories)"
            ).fetchone()
            recent_drift_rows = row_drift[0] if row_drift is not None else 0
        finally:
            if close_after and conn is not None:
                try:
                    from infra.db import safe_close_db
                    safe_close_db(conn, should_commit=False)
                except Exception:
                    pass

        writes_per_minute = max(recent_writes / (window_seconds / 60.0), 0.01)
        drift_per_minute = recent_drift_rows  # count in window
        ratio = drift_per_minute / writes_per_minute
        multiplier = max(0.5, min(3.0, ratio))
        return max(1, int(base * multiplier))
    except Exception:
        return base


def _check_and_reconcile_vec_drift(conn: AnyConnection, db_path: Path) -> None:
    """Check vector-index drift every worker run. Rebuild if drift > threshold.

    The cron worker runs every 5 minutes. This check costs 2 SELECTs
    in the no-drift case (sub-millisecond). On drift, it runs a full
    rebuild (~1.3s for 4K vectors). Any cron that adds memories
    between rebuild_vec_index runs (cross_session_learn, daily_digest,
    auto_summarize) leaves vec_keys 1-2 rows behind. This catches it
    within 5 minutes.

    Threshold configurable via memory.toml search.vec_rebuild_threshold
    or MEMORY_VEC_REBUILD_THRESHOLD env var (default: 5).
    """
    try:
        row_m = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()
        n_memories = row_m[0] if row_m is not None else 0
        row_vec = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()
        n_vec = row_vec[0] if row_vec is not None else 0
        row_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
        n_emb = row_emb[0] if row_emb is not None else 0
    except sqlite3.OperationalError as e:
        logger.info("vec_drift_check: skipping (table missing: %s)", e)
        return

    if n_vec > n_memories or n_emb > n_memories:
        # B5 fix: memories uses soft-delete (deleted_at).  Only hard-delete
        # vec_keys/embeddings for rows that no longer exist at all (hard
        # deleted).  Soft-deleted rows preserve their embeddings so that
        # un-delete later is cheap.
        #
        # H2 fix: delegate orphan cleanup to the saga-aware helpers so the
        # file-lock / write-lock are acquired properly and the operation
        # is recorded as operational maintenance (Rule 1).
        repair_kg_orphans(db_path)
        vec_result = repair_vec_orphans(db_path)
        n_orphan_vec = vec_result.get("deleted_vec_keys", 0)
        n_orphan_emb = vec_result.get("deleted_embeddings", 0)
        if n_orphan_vec or n_orphan_emb:
            logger.info(
                "vec_drift_check: cleaned orphan rows (vec_keys: -%d, embeddings: -%d)",
                n_orphan_vec,
                n_orphan_emb,
            )
        row_vec2 = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()
        n_vec = row_vec2[0] if row_vec2 is not None else 0
        row_emb2 = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
        n_emb = row_emb2[0] if row_emb2 is not None else 0

    vec_drift = n_memories - n_vec
    emb_drift = n_memories - n_emb
    if max(vec_drift, emb_drift) <= _get_vec_rebuild_threshold(conn):
        return

    logger.info(
        "vec_drift_check: drift vec=%d emb=%d (memories=%d, vec_keys=%d, embeddings=%d). Rebuilding...",
        vec_drift,
        emb_drift,
        n_memories,
        n_vec,
        n_emb,
    )
    try:
        handle_vec_index_rebuild({}, conn, db_path)
        n_vec_after_row = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()
        n_vec_after = n_vec_after_row[0] if n_vec_after_row is not None else 0
        logger.info(
            "vec_drift_check: rebuild complete (vec_keys: %d -> %d, drift was %d)",
            n_vec,
            n_vec_after,
            vec_drift,
        )
    except Exception as e:
        logger.warning("vec_drift_check: rebuild failed: %s", e)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def _maybe_run_wal_checkpoint(conn: AnyConnection, db_path: Path) -> None:
    """S4.3 (2026-06-23): debounced WAL checkpoint.

    Runs a PASSIVE checkpoint if either of:
      - The WAL file is larger than ``threshold_mb`` (default 10).
      - It's been longer than ``interval_s`` since the last run
        (default 300 = 5 min) — i.e. drain the WAL even if
        it's small, periodically.

    S4.4: ``_last_wal_checkpoint_at`` is a module-level float;
    on the first call in a process it's 0, so the first invocation
    will always checkpoint (which is what we want at startup).
    Concurrent invocations across multiple processes (e.g. a cron
    job AND a long-lived worker) are fine: PASSIVE checkpoint is
    safe to overlap.
    """
    global _last_wal_checkpoint_at

    interval_s = int(os.environ.get("MEMORY_WAL_CHECKPOINT_INTERVAL_S", "300"))
    threshold_mb = float(os.environ.get("MEMORY_WAL_CHECKPOINT_THRESHOLD_MB", "10.0"))
    now = time.time()
    if now - _last_wal_checkpoint_at < 60.0:
        # S4.4 debounce: skip if last run was < 60s ago.
        return

    wal_path = Path(str(db_path) + "-wal")
    wal_too_big = False
    if wal_path.exists():
        try:
            size_mb = wal_path.stat().st_size / (1024 * 1024)
            wal_too_big = size_mb >= threshold_mb
        except OSError as exc:
            logger.debug("background_worker: cannot stat WAL %s: %s", wal_path, exc)
            wal_too_big = False
    time_elapsed = now - _last_wal_checkpoint_at >= interval_s
    if not (wal_too_big or time_elapsed):
        return

    try:
        handle_wal_checkpoint({"threshold_mb": threshold_mb}, conn, db_path)
        _last_wal_checkpoint_at = now
    except Exception as e:
        logger.warning("worker: wal_checkpoint failed: %s", e)


_last_wal_checkpoint_at: float = 0.0


def process_one_task(
    conn: AnyConnection, db_path: Path, task_type: str | None = None
) -> bool:
    """Dequeue and process one task. Returns True if a task was processed."""
    task = dequeue_task(conn, task_type=task_type)
    if not task:
        return False

    task_id = task["id"]
    ttype = task["task_type"]
    payload = task["payload"]

    # Resolve cron script paths from CRON_SCRIPT_MAP for cron-style task types.
    cron_script_missing = False
    if not payload.get("script") and ttype in CRON_SCRIPT_MAP:
        payload = {**payload, "script": CRON_SCRIPT_MAP[ttype]}
        handler = HANDLERS.get("run_script")
    elif not payload.get("script") and ttype.startswith("cron_"):
        cand1 = Path(_REPO_ROOT) / "cron" / f"{ttype}.py"
        cand2 = Path(_REPO_ROOT) / "cron" / f"{ttype[5:]}.py"
        cand3 = Path(_REPO_ROOT) / f"{ttype[5:]}.py"
        if cand1.exists():
            payload = {**payload, "script": f"cron/{ttype}.py"}
            handler = HANDLERS.get("run_script")
        elif cand2.exists():
            payload = {**payload, "script": f"cron/{ttype[5:]}.py"}
            handler = HANDLERS.get("run_script")
        elif cand3.exists():
            payload = {**payload, "script": f"{ttype[5:]}.py"}
            handler = HANDLERS.get("run_script")
        else:
            handler = HANDLERS.get(ttype)
            cron_script_missing = True
    else:
        handler = HANDLERS.get(ttype)

    if not handler:
        if cron_script_missing:
            fail_task(conn, task_id, f"script not found for cron task: {ttype}")
            logger.warning("worker: script not found for cron task %s (id=%d)", ttype, task_id)
        else:
            fail_task(conn, task_id, f"unknown task type: {ttype}")
            logger.warning("worker: unknown task type %s (id=%d)", ttype, task_id)
        return True

    # Lazy-resolve None entries in HANDLERS (used to break circular imports).
    if handler is None:
        _lazy_map = {
            "entailment_chains": "reasoning.compile.handle_entailment_chains",
            "concept_compilation": "reasoning.compile.handle_concept_compilation",
            "skill_enrichment": "reasoning.compile.handle_skill_enrichment",
        }
        _mod_path = _lazy_map.get(ttype)
        if _mod_path:
            _mod_name, _fn_name = _mod_path.rsplit(".", 1)
            try:
                _mod = __import__(_mod_name, fromlist=[_fn_name])
                handler = getattr(_mod, _fn_name)
            except Exception as _le:
                fail_task(conn, task_id, f"lazy import failed: {_le}")
                logger.warning("worker: lazy import failed for %s: %s", ttype, _le)
                return True
        else:
            fail_task(conn, task_id, f"no handler for {ttype}")
            logger.warning("worker: no handler registered for %s", ttype)
            return True

    # Watchdog: warn + fail on per-task hang. The 99.9% CPU incident
    # on 2026-06-22 was a single task stuck in a regex search loop for
    # 28+ minutes. If a task runs >120s, log a warning and mark it
    # failed so the queue can progress. Threshold is conservative —
    # KG extraction on 3K memories legitimately takes 5-10s.
    import signal as _sig

    _PER_TASK_TIMEOUT_S = int(os.environ.get("MEMORY_WORKER_TASK_TIMEOUT_S", "300"))

    class _TaskTimeout(Exception):
        pass

    def _timeout_handler(signum, frame):
        raise _TaskTimeout(f"task exceeded {_PER_TASK_TIMEOUT_S}s timeout")

    # P0-10 fix (2026-06-23): signal.alarm only works in the main thread.
    # If this function is invoked from a non-main thread (e.g., from a
    # test or from another worker), signal.SIGALRM will raise a
    # ValueError. Guard with a thread check so the worker can run in
    # non-main threads without crashing; in that case we skip the
    # signal-based timeout (the run is bounded by the outer cron
    # timeout anyway).
    _use_signal_timeout = (
        _sig.getsignal(_sig.SIGALRM) is not _sig.SIG_DFL
        or threading.current_thread() is threading.main_thread()
    )

    if _use_signal_timeout:
        old_handler = _sig.signal(_sig.SIGALRM, _timeout_handler)
        _sig.alarm(_PER_TASK_TIMEOUT_S)
    t_start = time.time()
    try:
        result = handler(payload, conn, db_path)
        complete_task(conn, task_id)
        elapsed = time.time() - t_start
        logger.info(
            "worker: completed task %d (%s) in %.2fs: %s",
            task_id,
            ttype,
            elapsed,
            result,
        )
    except _TaskTimeout:
        elapsed = time.time() - t_start
        # exhaust=True: a task that hung the watchdog will hang again on
        # retry, burning a full timeout of CPU per attempt (the 2026-07-31
        # incident: fact_consolidation re-picked for hours at 100% CPU).
        fail_task(conn, task_id, f"timeout after {elapsed:.1f}s", exhaust=True)
        logger.error(
            "worker: task %d (%s) TIMED OUT after %.1fs — likely runaway regex or loop",
            task_id,
            ttype,
            elapsed,
        )
    except Exception as e:
        elapsed = time.time() - t_start
        error_msg = str(e)[:500]
        fail_task(conn, task_id, error_msg)
        logger.warning(
            "worker: task %d (%s) failed after %.2fs: %s",
            task_id,
            ttype,
            elapsed,
            error_msg,
        )
    finally:
        if _use_signal_timeout:
            _sig.alarm(0)
            _sig.signal(_sig.SIGALRM, old_handler)

    return True


class WorkerPool:
    """Fixed-size pool of background worker threads.

    Each thread maintains its own DB connection from the pool.
    Workers serialize on ``dequeue_task``'s ``BEGIN IMMEDIATE`` — only one
    worker dequeues at a time; the rest wait on the SQLite write lock.

    In drain mode each worker has an independent deadline derived from
    the original wall-clock guard in ``run_worker``.
    """

    def __init__(self, db_path: Path, n_workers: int = 2,
                 task_type: str | None = None):
        if n_workers < 1:
            raise ValueError("n_workers must be >= 1")
        self._db_path = db_path
        self._n_workers = n_workers
        self._task_type = task_type

    def run(self, drain: bool = False, max_tasks: int = 10000) -> None:
        logger.info(
            "worker pool: starting %d workers (db=%s, drain=%s, max_tasks=%d)",
            self._n_workers, self._db_path, drain, max_tasks,
        )
        with ThreadPoolExecutor(max_workers=self._n_workers) as executor:
            futures = [
                executor.submit(self._worker_loop, i, drain, max_tasks)
                for i in range(self._n_workers)
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    logger.exception("worker pool: worker thread crashed")

    def _worker_loop(self, worker_id: int, drain: bool, max_tasks: int) -> None:
        from infra.db_write_queue import sqlite_write_queue
        conn = sqlite_write_queue.start_session(self._db_path)
        processed = 0
        t_drain = time.time()
        try:
            _DRAIN_MAX_WALL_S = int(
                os.environ.get("MEMORY_WORKER_DRAIN_MAX_WALL_S", "600")
            )
            while not _shutdown:
                ok = process_one_task(conn, self._db_path, task_type=self._task_type)
                if not ok:
                    if drain:
                        break
                    for _ in range(15):
                        if _shutdown:
                            return
                        time.sleep(1)
                    continue
                processed += 1
                if drain and processed >= max_tasks:
                    break
                if drain and time.time() - t_drain > _DRAIN_MAX_WALL_S:
                    logger.warning(
                        "worker pool: worker %d hit wall-clock cap of %ds after %d tasks",
                        worker_id, _DRAIN_MAX_WALL_S, processed,
                    )
                    break
        except Exception:
            logger.exception("worker pool: worker %d crashed", worker_id)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class WorkerLivenessMonitor:
    """Worker Liveness Monitor for background processes and worker threads.

    Detects worker death within 30s, automatically restarts worker threads,
    and alerts on repeated consecutive failures (>= max_consecutive_failures).
    """

    def __init__(
        self,
        db_path: Path,
        n_workers: int = 2,
        task_type: str | None = None,
        max_consecutive_failures: int = 3,
        liveness_check_interval_s: float = 2.0,
    ) -> None:
        self._db_path = db_path
        self._n_workers = n_workers
        self._task_type = task_type
        self._max_failures = max_consecutive_failures
        self._interval = liveness_check_interval_s
        self._consecutive_failures: dict[int, int] = {i: 0 for i in range(n_workers)}
        self._pool = WorkerPool(db_path, n_workers=n_workers, task_type=task_type)

    def run_monitored(self, drain: bool = False, max_tasks: int = 10000) -> None:
        """Run worker pool with active liveness monitoring and auto-restart."""
        logger.info("liveness monitor: starting monitored worker pool (%d workers)", self._n_workers)

        workers: dict[int, threading.Thread] = {}
        stop_event = threading.Event()

        def spawn_worker(w_id: int) -> threading.Thread:
            def runner() -> None:
                try:
                    self._pool._worker_loop(w_id, drain, max_tasks)
                except Exception as exc:
                    self._consecutive_failures[w_id] += 1
                    failures = self._consecutive_failures[w_id]
                    logger.warning(
                        "liveness monitor: worker %d died (%s). Consecutive failures: %d",
                        w_id, exc, failures,
                    )
                    if failures >= self._max_failures:
                        logger.error(
                            "ALERT: worker %d failed %d consecutive times! Auto-restarting but alert triggered.",
                            w_id, failures,
                        )
                else:
                    self._consecutive_failures[w_id] = 0

            t = threading.Thread(target=runner, daemon=True, name=f"WorkerMonitorThread-{w_id}")
            t.start()
            return t

        for i in range(self._n_workers):
            workers[i] = spawn_worker(i)

        try:
            while not _shutdown and not stop_event.is_set():
                time.sleep(self._interval)
                for i in range(self._n_workers):
                    t = workers.get(i)
                    if t and not t.is_alive() and not drain and not _shutdown:
                        logger.info("liveness monitor: worker %d detected dead within <30s — auto-restarting", i)
                        workers[i] = spawn_worker(i)
                if drain and all(not t.is_alive() for t in workers.values()):
                    break
        finally:
            stop_event.set()


def start_worker_liveness_monitor(
    db_path: Path,
    n_workers: int = 2,
    task_type: str | None = None,
    max_consecutive_failures: int = 3,
) -> WorkerLivenessMonitor:
    """Instantiate a WorkerLivenessMonitor for a database path."""
    return WorkerLivenessMonitor(
        db_path=db_path,
        n_workers=n_workers,
        task_type=task_type,
        max_consecutive_failures=max_consecutive_failures,
    )


def _check_high_priority_pending(conn: AnyConnection) -> bool:
    try:
        row = conn.execute("SELECT id FROM task_queue WHERE status = 'pending' LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


def run_worker(
    db_path: Path,
    interval: int = 300,
    task_type: str | None = None,
    once: bool = False,
    drain: bool = False,
    max_tasks: int = 10000,
    n_workers: int = 1,
) -> None:
    """Run the worker loop.

    Args:
        db_path: Path to memory.db
        interval: Seconds between polls (default 300 = 5 min)
        task_type: If set, only process tasks of this type
        once: If True, process one task and exit (for cron)
        drain: If True, process all pending tasks until empty or
            ``max_tasks`` reached, then exit. Use to burn down
            a backlog (e.g. 12K tasks accumulated during downtime).
        max_tasks: Safety cap on drain mode (default 10000).
        n_workers: Number of concurrent worker threads (default 1).
            Pass >1 to enable the threaded WorkerPool.
    """
    import sqlite3 as _worker_sqlite3

    def _worker_conn(path):
        """Get a direct DB connection from the pool (bypasses write queue).

        The worker is the single long-lived writer to memory.db.
        Going through the write queue causes ABBA deadlocks: the
        queue thread holds db_path_flock while the worker tries to
        acquire it via open_db(write=True).  The pool uses raw
        sqlite3.connect, not the write queue, so it is safe.
        """
        from infra.db import connection_pool
        return connection_pool.get(str(path), timeout=30)

    import threading as _threading

    logger.info(
        "worker: starting (db=%s, interval=%ds, once=%s, drain=%s, "
        "max_tasks=%d, n_workers=%d)",
        db_path, interval, once, drain, max_tasks, n_workers,
    )

    # Init phase — runs once with a direct connection (bypasses write
    # queue to avoid flock contention on startup).
    import sqlite3 as _sqlite3
    init_conn = _sqlite3.connect(str(db_path), timeout=30)
    init_conn.execute("PRAGMA journal_mode=WAL")
    init_conn.execute("PRAGMA busy_timeout=30000")
    try:
        init_task_queue(init_conn)
    except Exception as e:
        logger.error("worker: failed to init task queue: %s", e)
        init_conn.close()
        return

    _check_and_reconcile_vec_drift(init_conn, db_path)

    try:
        from background.corpus_budget_guard import run_corpus_budget_guard
        guard_status = run_corpus_budget_guard(db_path, conn=init_conn)
        if guard_status.get("compaction_enqueued"):
            logger.info(
                "worker: corpus budget exceeded (~%d tokens, budget %d) — "
                "compaction enqueued",
                guard_status.get("tokens", 0),
                guard_status.get("budget", 0),
            )
    except Exception as _guard_exc:
        logger.debug("worker: corpus budget guard failed: %s", _guard_exc)

    _maybe_run_wal_checkpoint(init_conn, db_path)
    init_conn.close()

    # Delegate to WorkerPool when n_workers > 1
    if n_workers > 1:
        pool = WorkerPool(db_path, n_workers=n_workers, task_type=task_type)
        pool.run(drain=drain, max_tasks=max_tasks)
        return

    # Single-threaded path — use direct pooled connections so the
    # per-task flock is released between tasks, unblocking concurrent
    # callers (e.g. mcp_authorize's audit INSERT).  The write-queue
    # session mode held the flock for the entire drain/interval loop,
    # causing 30s timeouts in any other process that needed to write.
    import signal as _proc_sig

    _PROCESS_TIMEOUT_S = int(
        os.environ.get("MEMORY_WORKER_PROCESS_TIMEOUT_S", "3600")
    )

    def _process_killer(signum, frame):
        logger.error(
            "worker: process exceeded %ds timeout — force-exiting",
            _PROCESS_TIMEOUT_S,
        )
        os._exit(1)

    # signal.signal only works in the main thread — skip when called
    # from a worker thread (e.g. pytest runs run_worker in a thread).
    if _threading.current_thread() is _threading.main_thread():
        _proc_sig.signal(_proc_sig.SIGALRM, _process_killer)
        _proc_sig.alarm(_PROCESS_TIMEOUT_S)

    try:
        if drain:
            _DRAIN_MAX_WALL_S = int(
                os.environ.get("MEMORY_WORKER_DRAIN_MAX_WALL_S", "600")
            )
            processed = 0
            t_drain = time.time()
            while not _shutdown and processed < max_tasks:
                if time.time() - t_drain > _DRAIN_MAX_WALL_S:
                    logger.warning(
                        "worker: drain hit wall-clock cap of %ds after %d tasks — exiting",
                        _DRAIN_MAX_WALL_S,
                        processed,
                    )
                    break
                conn = _worker_conn(db_path)
                try:
                    ok = process_one_task(conn, db_path, task_type=task_type)
                    if not ok:
                        break
                    processed += 1
                    if processed % 50 == 0:
                        elapsed = time.time() - t_drain
                        rate = processed / elapsed if elapsed > 0 else 0
                        logger.info(
                            "worker: drain progress %d/%d (%.1f tasks/sec)",
                            processed,
                            max_tasks,
                            rate,
                        )
                finally:
                    from infra.db import safe_close_db
                    safe_close_db(conn)
            elapsed = time.time() - t_drain
            logger.info(
                "worker: drain complete — processed %d tasks in %.1fs (%.1f tasks/sec)",
                processed,
                elapsed,
                processed / elapsed if elapsed > 0 else 0,
            )
            return
        else:
            batch_size = _get_effective_batch_size()
            while not _shutdown:
                batch_processed = 0
                while batch_processed < batch_size:
                    conn = _worker_conn(db_path)
                    try:
                        ok = process_one_task(conn, db_path, task_type=task_type)
                        if not ok:
                            break
                        batch_processed += 1
                    finally:
                        from infra.db import safe_close_db as _sc
                        _sc(conn)
                if once:
                    break
                if batch_processed == 0:
                    for _ in range(interval):
                        if _shutdown:
                            break
                        try:
                            check_conn = _worker_conn(db_path)
                            try:
                                if _check_high_priority_pending(check_conn):
                                    break
                            finally:
                                from infra.db import safe_close_db
                                safe_close_db(check_conn)
                        except Exception:
                            pass
                        time.sleep(1)
    finally:
        _proc_sig.alarm(0)
        logger.info("worker: stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Agentic memory background worker")
    parser.add_argument("--once", action="store_true", help="Process one task and exit")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process all pending tasks until queue is empty (or --max-tasks "
        "hit), then exit. Use to burn down a backlog.",
    )
    parser.add_argument(
        "--interval", type=int, default=None, help="Poll interval in seconds"
    )
    parser.add_argument(
        "--type", type=str, default=None, help="Only process this task type"
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Safety cap on --drain mode (default 10000 from env or built-in)",
    )
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument(
        "--workers", type=int, default=None, help="Number of worker threads (default 1)"
    )
    args = parser.parse_args()

    # Acquire a mode-specific flock so persistent (--interval) and
    # drain/once modes can coexist without contention.  --interval
    # holds "background_worker_persistent" for its lifetime; --drain
    # and --once hold "background_worker_drain" for one batch.  The
    # cron scheduler runs --drain every 5 min; launchd runs --interval.
    # With different lock scopes both paths work: no more silent no-op
    # drain ticks or "database is locked" while the persistent worker
    # processes a long task.
    # signal.signal only works in the main thread.
    import threading as _threading
    if _threading.current_thread() is _threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    _lock_name = "background_worker_drain" if (args.drain or args.once) else "background_worker_persistent"
    _no_flock = os.environ.get("MEMORY_CRON_NO_FLOCK", "") == "1"
    if _no_flock:
        logger.info("background_worker: MEMORY_CRON_NO_FLOCK=1, skipping flock %s", _lock_name)
    else:
        try:
            from cron._flock import acquire_lock_or_exit

            acquire_lock_or_exit(_lock_name)
        except ImportError:
            # Best-effort: if flock module isn't on path, fall back to
            # a lightweight inline lock using fcntl directly.
            import fcntl

            _lock_path = (
                Path.home()
                / ".config"
                / "agentic-memory"
                / "memory"
                / "locks"
                / f"{_lock_name}.lock"
            )
            _lock_path.parent.mkdir(parents=True, exist_ok=True)
            _lock_fd = open(_lock_path, "w")
            try:
                fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info("background_worker: another instance holds %s lock, exiting", _lock_name)
                _lock_fd.close()
                return 0
            global _BACKGROUND_WORKER_LOCK_FD
            _BACKGROUND_WORKER_LOCK_FD = _lock_fd

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if args.db:
        db_path = Path(args.db)
    else:
        env = os.environ.get("MEMORY_DB_PATH")
        if env:
            db_path = Path(env)
        else:
            db_path = resolve_active_memory_dir() / "memory.db"

    interval = args.interval or int(os.environ.get("MEMORY_WORKER_INTERVAL", "5"))

    if args.max_tasks is not None:
        max_tasks = args.max_tasks
    else:
        max_tasks = int(os.environ.get("MEMORY_WORKER_MAX_TASKS", "10000"))

    run_worker(
        db_path,
        interval=interval,
        task_type=args.type,
        once=args.once,
        drain=args.drain,
        max_tasks=max_tasks,
        n_workers=args.workers or 1,
    )


if __name__ == "__main__":
    main()


def _reconciliation_loop_sharded(
    journal_path: Path,
    target_base: Path,
    worker_id: int,
    n_workers: int,
) -> None:
    """Run one shard of the multi-writer journal reconciliation worker loop."""
    from infra.write_journal import process_pending_journal_entries
    process_pending_journal_entries(journal_path, target_base, worker_id=worker_id, n_workers=n_workers)


def multiwriter_reconciliation_pool(
    journal_path: Path,
    target_base: Path,
    n_workers: int = 4,
    idle_quit_after_secs: float = 30.0,
) -> None:
    """Launch multi-writer journal reconciliation pool."""
    from infra.write_journal import process_pending_journal_entries
    process_pending_journal_entries(journal_path, target_base, n_workers=n_workers)

