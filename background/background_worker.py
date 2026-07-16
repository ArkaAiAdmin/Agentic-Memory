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
from background.background_queue import init_task_queue, dequeue_task, complete_task, fail_task, reset_stuck_processing_tasks
from infra.infrastructure import resolve_active_memory_dir
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False

# Grace period (seconds) for in-flight task to complete after SIGTERM/SIGINT
_SHUTDOWN_GRACE_S = int(os.environ.get("MEMORY_WORKER_SHUTDOWN_GRACE_S", "10"))

# LLM fact-extractor guard.  When True, the worker skips any task that
# requires the LLM extractor (Qwen/Qwen2.5-3B-Instruct, ~2 GB mmap +
# torch/tokenizers/rayon).  Set automatically for drain/once/short-lived
# modes (the worker only needs to flush the write journal in those modes),
# or explicitly via --no-extractor or MEMORY_LLM_EXTRACTION=0.
_EXTRACTOR_DISABLED = False

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
    except Exception as _wp_exc:
        logger.warning("_get_effective_batch_size: broad except swallowed: %s", _wp_exc)
        return _DEFAULT_BATCH_SIZE

# Module-level keep-alive for the inline flock fd (H-fix 2026-06-22).
# See the ImportError fallback in main() — if cron._flock isn't on
# path, we acquire fcntl.flock directly and must keep the fd alive
# for the worker's lifetime or the lock is released.
_BACKGROUND_WORKER_LOCK_FD = None


def _handle_signal(signum, frame):
    global _shutdown
    if _shutdown:
        return
    _shutdown = True
    _RECONCILER_SHUTDOWN.set()
    logger.info(
        "worker: received signal %d — shutting down after current task "
        "(%ds grace before force-exit)",
        signum, _SHUTDOWN_GRACE_S,
    )
    # Give in-flight tasks time to finish their DB writes
    threading.Thread(
        target=_shutdown_force_exit,
        daemon=True,
        name="shutdown-force-exit",
    ).start()


def _shutdown_force_exit() -> None:
    """Force-exit the worker process if grace period expires."""
    import time
    time.sleep(_SHUTDOWN_GRACE_S)
    if _shutdown:
        logger.warning(
            "worker: grace period of %ds expired — force-exiting",
            _SHUTDOWN_GRACE_S,
        )
        os._exit(0)


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
    if _EXTRACTOR_DISABLED:
        return "skipped: extractor disabled"
    try:
        from pathlib import Path as _P
        from fact.consolidate_facts import consolidate_memory_facts

        # Guard: skip if corpus is too large (>2000 notes) — consolidate_facts
        # uses O(n²) contradiction detection and will return immediately with
        # a warning, but the module-level imports (llm_extraction, sentence
        # transformers) still happen at import time and can load a 3B LLM
        # consuming 6-8GB. Check + short-circuit here to skip the expensive
        # import entirely when the guard would immediately return.
        row = conn.execute("SELECT COUNT(*) FROM tenant_memories WHERE deleted_at IS NULL").fetchone()
        n = int(row[0]) if row else 0
        if n > 2000:
            raise RuntimeError(
                f"corpus {n} notes exceeds consolidation guard (2000) "
                f"— run compaction manually or increase guard"
            )
        try:
            consolidate_memory_facts(db_path=_P(db_path))
        except Exception as _wp_exc:
            logger.warning("handle_fact_consolidation: broad except swallowed: %s", _wp_exc)
            pass
        return "fact consolidation completed"
    except Exception as e:
        raise RuntimeError(f"fact_consolidation failed: {e}") from e


def handle_semantic_backlinks(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Create semantic KG edges between the saved memory and its nearest neighbors."""
    if _EXTRACTOR_DISABLED:
        return "skipped: extractor disabled"
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


def handle_colbert_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Index ColBERT token embeddings for a memory note (deferred from save)."""
    try:
        from save.indexers import _index_colbert

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = payload.get("category", "") or (memory_id.split("/")[0] if "/" in memory_id else "general")
        tags = payload.get("tags", [])
        source_file = payload.get("source_file", "")
        _index_colbert(conn, memory_id, content, category, tags, source_file)
        conn.commit()
        return f"colbert indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"colbert_index failed: {e}") from e


def handle_splade_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Index SPLADE sparse vectors for a memory note (deferred from save)."""
    try:
        from save.indexers import _index_splade

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = payload.get("category", "") or (memory_id.split("/")[0] if "/" in memory_id else "general")
        tags = payload.get("tags", [])
        source_file = payload.get("source_file", "")
        _index_splade(conn, memory_id, content, category, tags, source_file)
        conn.commit()
        return f"splade indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"splade_index failed: {e}") from e


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
        category = payload.get("category", "") or (memory_id.split("/")[0] if "/" in memory_id else "general")
        tags = payload.get("tags", [])
        _index_embedding(conn, memory_id, content, category, tags, source_file)
        conn.commit()
        return f"embedding indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"embedding_index failed: {e}") from e


def handle_kg_and_fact_index(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    """Extract KG entities, facts, and enrich context for a memory (deferred)."""
    if _EXTRACTOR_DISABLED:
        return "skipped: extractor disabled"
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
    # Release SQLite RESERVED lock + per-DB-path flock before the
    # subprocess so the child can write to the DB without blocking.
    if hasattr(conn, "commit_release"):
        conn.commit_release()
    if hasattr(conn, "release_flock"):
        conn.release_flock()
    try:
        result = subprocess.run(
            [str(venv_py), str(script), *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    finally:
        if hasattr(conn, "acquire_flock"):
            conn.acquire_flock()
        if hasattr(conn, "acquire_lock"):
            conn.acquire_lock()
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
    "colbert_index": handle_colbert_index,
    "splade_index": handle_splade_index,
    "embedding_index": handle_embedding_index,
    "chunk_embedding_index": handle_chunk_embedding_index,
    "kg_and_fact_index": handle_kg_and_fact_index,
    "semantic_backlinks": handle_semantic_backlinks,
    "vec_index_rebuild": handle_vec_index_rebuild,
    "wal_checkpoint": handle_wal_checkpoint,
    "run_script": handle_run_script,
    "evidence_chain_staleness": handle_evidence_chain_staleness,
    "cron_pipeline_sentinel": lambda payload, conn, db_path: "pipeline_healthy",
}


def _lazy_entailment_chains(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from config import get_config

    _cfg = get_config()
    if not getattr(_cfg, 'knowledge_compilation', True):
        return "entailment_chains: disabled (MEMORY_KNOWLEDGE_COMPILATION=0)"
    from reasoning.compile import handle_entailment_chains
    return handle_entailment_chains(payload, conn, db_path)


def _lazy_concept_compilation(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from config import get_config

    _cfg = get_config()
    if not getattr(_cfg, 'knowledge_compilation', True):
        return "concept_compilation: disabled (MEMORY_KNOWLEDGE_COMPILATION=0)"
    from reasoning.compile import handle_concept_compilation
    return handle_concept_compilation(payload, conn, db_path)


def _lazy_skill_enrichment(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from config import get_config

    _cfg = get_config()
    if not getattr(_cfg, 'knowledge_compilation', True):
        return "skill_enrichment: disabled (MEMORY_KNOWLEDGE_COMPILATION=0)"
    from reasoning.compile import handle_skill_enrichment
    return handle_skill_enrichment(payload, conn, db_path)


def _lazy_graph_communities(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from config import get_config

    _cfg = get_config()
    if not getattr(_cfg, 'graph_communities', True):
        return "graph_communities: disabled (MEMORY_GRAPH_COMMUNITIES=0)"
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


def _lazy_graph_snapshots(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from kg.graph_analytics import compute_pagerank
    from kg.graph_communities import connected_components
    import json as _json
    import time as _time

    now = _time.time()
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
        except Exception as _wp_exc:
            logger.warning("_lazy_graph_snapshots: broad except swallowed: %s", _wp_exc)
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


def _lazy_revalidate_entailments(
    payload: dict, conn: AnyConnection, db_path: Path
) -> str:
    from reasoning.compile import revalidate_entailment_chains

    dry_run = bool(payload.get("dry_run", False))
    batch_size = int(payload.get("batch_size", 500))
    result = revalidate_entailment_chains(
        conn, db_path, dry_run=dry_run, batch_size=batch_size
    )
    return (
        f"revalidate_entailments: checked={result['checked']} "
        f"invalidated={result['invalidated']} errors={result['errors']}"
    )


def _lazy_resolve_contradictions(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from cron.cron_resolve_contradictions import main
    main()
    return "resolve_contradictions: completed"


def _lazy_review_beliefs(payload: dict, conn: AnyConnection, db_path: Path) -> str:
    from cron.cron_review_beliefs import main
    main()
    return "review_beliefs: completed"


HANDLERS.update(
    {
        "entailment_chains": _lazy_entailment_chains,
        "concept_compilation": _lazy_concept_compilation,
        "skill_enrichment": _lazy_skill_enrichment,
        "graph_communities": _lazy_graph_communities,
        "graph_snapshots": _lazy_graph_snapshots,
        "revalidate_entailments": _lazy_revalidate_entailments,
        "cron_resolve_contradictions": _lazy_resolve_contradictions,
        "cron_review_beliefs": _lazy_review_beliefs,
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
    # Phase B v2 — Step 4: converted from direct-script to enqueue_task
    "cron_health_check": "cron/cron_health_check.py",
    "cron_policy_hash_status": "cron/cron_policy_hash_status.py",
    "cron_check_config_drift": "cron/cron_check_config_drift.py",
    "cron_train_forget_model": "cron/cron_train_forget_model.py",
    "cron_train_temporal_ssm": "cron/cron_train_temporal_ssm.py",
    "cron_train_ltr": "cron/cron_train_ltr.py",
    "cron_auto_retry_dead_tasks": "cron/cron_retry_dead_tasks.py",
    # Pre-Phase B — already mapped
    # Z-7 fix: rename cron/cleanup_auto_logs.py → cron/cron_cleanup_auto_logs.py
    # to match the cron_*.py naming convention. Old path kept as fallback
    # for any lingering direct invocations that bypass the task queue.
    "cron_cleanup_auto_logs": "cron/cron_cleanup_auto_logs.py",
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
    "cron_promote_drafts": "cron/cron_promote_drafts.py",
    "cron_semantic_clusters": "cron/cron_semantic_clusters.py",
    "cron_skill_decay": "cron/cron_skill_decay.py",
    "cron_review_beliefs": "cron/cron_review_beliefs.py",
}


# ---------------------------------------------------------------------------
# Proactive vec-index drift reconciliation
# ---------------------------------------------------------------------------


def _get_vec_rebuild_threshold() -> int:
    """Return the max allowable drift before auto-rebuild.

    Reads from config (memory.toml → search.vec_rebuild_threshold,
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
    except Exception as _wp_exc:
        logger.warning("_get_vec_rebuild_threshold: broad except swallowed: %s", _wp_exc)
        base = int(os.environ.get("MEMORY_VEC_REBUILD_THRESHOLD", "15"))
        adaptive = False

    if not adaptive or base <= 0:
        return base

    try:
        import sqlite3
        from infra._lazy_imports import get_memory_paths

        _, mem_dir, _ = get_memory_paths()
        db_path = mem_dir / "memory.db"
        if not db_path.exists():
            return base
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.row_factory = sqlite3.Row
        window_seconds = 600  # 10-minute window for rate estimation
        recent_writes = conn.execute(
            "SELECT COUNT(*) AS n FROM tenant_memories "
            "WHERE updated_at >= datetime('now', ?)",
            (f"-{window_seconds} seconds",),
        ).fetchone()["n"]
        recent_drift_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_vec_keys "
            "WHERE memory_id NOT IN (SELECT id FROM tenant_memories)"
        ).fetchone()["n"]
        conn.close()
        writes_per_minute = max(recent_writes / (window_seconds / 60.0), 0.01)
        drift_per_minute = recent_drift_rows  # count in window
        ratio = drift_per_minute / writes_per_minute
        multiplier = max(0.5, min(3.0, ratio))
        return max(1, int(base * multiplier))
    except Exception as _wp_exc:
        logger.warning("_get_vec_rebuild_threshold: broad except swallowed: %s", _wp_exc)
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
            "SELECT COUNT(*) FROM tenant_memories WHERE deleted_at IS NULL"
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
        n_orphan_vec = conn.execute(
            "DELETE FROM memory_vec_keys WHERE memory_id NOT IN "
            "(SELECT id FROM tenant_memories)"
        ).rowcount
        n_orphan_emb = conn.execute(
            "DELETE FROM memory_embeddings WHERE memory_id NOT IN "
            "(SELECT id FROM tenant_memories)"
        ).rowcount
        if n_orphan_vec or n_orphan_emb:
            conn.commit()
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
    if max(vec_drift, emb_drift) <= _get_vec_rebuild_threshold():
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
# CQRS write-journal reconciliation loop (2026-07-07)
# ---------------------------------------------------------------------------


_RECONCILER_SHUTDOWN = threading.Event()


def _reconciliation_loop(journal_path: Path, target_base: Path) -> None:
    """Continuous loop: poll the write journal and apply pending entries.

    This is the ONLY writer to the main DB.  It serialises journal
    entries through ``materialize_journal_entry`` which runs the
    existing saga (DB upsert + vec key + file write + post-save hooks).

    The loop polls every 100ms.  On shutdown signal it finishes the
    current batch and returns.
    """
    from save.pipeline import materialize_journal_entry

    while not _RECONCILER_SHUTDOWN.is_set():
        try:
            from infra.write_journal import (
                dequeue_pending,
                reset_stuck_processing,
            )

            # Reset stuck entries first (handles daemon crash mid-batch).
            # 2026-07-08: reset_stuck_processing now unconditionally closes
            # the thread-local journal transaction (see write_journal.py),
            # so we don't need extra cleanup here — but we still wrap in
            # try/finally for resilience against future code paths.
            try:
                reset_stuck_processing(journal_path)
            except Exception as _rs_err:
                logger.warning("reconciliation: reset_stuck_processing failed: %s", _rs_err)

            try:
                entries = dequeue_pending(journal_path, batch_size=10)
            except Exception as _dq_err:
                logger.warning("reconciliation: dequeue_pending failed: %s", _dq_err)
                _RECONCILER_SHUTDOWN.wait(0.1)
                continue
            if not entries:
                # No work → sleep 100ms before next poll
                _RECONCILER_SHUTDOWN.wait(0.1)
                continue

            for entry in entries:
                if _RECONCILER_SHUTDOWN.is_set():
                    break
                try:
                    materialize_journal_entry(entry, target_base, journal_path)
                except Exception as exc:
                    logger.exception(
                        "reconciliation: entry %d (%s) failed: %s",
                        entry.get("id"),
                        entry.get("note_id", "?"),
                        exc,
                    )
                # Yield to the scheduler between entries so the shutdown
                # signal is observed promptly during large batch drains.
                _RECONCILER_SHUTDOWN.wait(0.001)
        except Exception as loop_exc:
            logger.error("reconciliation loop error: %s", loop_exc)
            _RECONCILER_SHUTDOWN.wait(1.0)

    logger.info("reconciliation loop: stopped")


def _start_reconciler(journal_path: Path, target_base: Path) -> threading.Thread:
    """Start the reconciliation daemon thread."""
    _RECONCILER_SHUTDOWN.clear()
    thread = threading.Thread(
        target=_reconciliation_loop,
        args=(journal_path, target_base),
        daemon=True,
        name="journal-reconciler",
    )
    thread.start()
    logger.info("reconciliation loop: started (journal=%s, target=%s)", journal_path, target_base)
    return thread


# ---------------------------------------------------------------------------
# Multi-worker reconciler fleet (opt-in via MEMORY_RECONCILER_N_WORKERS > 1)
# ---------------------------------------------------------------------------


def _journal_is_globally_drained(journal_path: Path) -> bool:
    """Return True if the journal has no pending or processing entries."""
    try:
        conn = sqlite3.connect(str(journal_path), timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS p, "
            "  SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS pr "
            "FROM write_journal"
        ).fetchone()
        conn.close()
        if row is None:
            return True
        pending = int(row[0])
        processing = int(row[1])
        return pending == 0 and processing == 0
    except Exception as _jd_err:
        logger.debug("global drained check failed: %s", _jd_err)
        return False


def _reconciliation_loop_sharded(
    journal_path: Path,
    target_base: Path,
    worker_id: int,
    n_workers: int,
) -> None:
    """Per-worker reconciler loop with id-sharded journal claim.

    Each worker claims rows whose ``id % n_workers == worker_id`` so
    N workers operate on disjoint journal slices.  Shutdown is handled
    via the module-level ``_RECONCILER_SHUTDOWN`` event (set by the
    parent process supervisor before terminating children).
    """
    from save.pipeline import materialize_journal_entry
    from infra.write_journal import (
        dequeue_pending_for_worker,
        reset_stuck_processing,
    )

    _shutdown_local = threading.Event()

    def _on_term(signum, frame):
        _shutdown_local.set()

    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except (ValueError, OSError):
        pass

    _IDLE_EXIT_EMPTIES = 5  # 5 consecutive empty dequeues (~0.5s)
    _empty_dequeues = 0

    while not _shutdown_local.is_set() and not _RECONCILER_SHUTDOWN.is_set():
        try:
            try:
                reset_stuck_processing(journal_path)
            except Exception as _rs_err:
                logger.warning("reconciler-%d: reset_stuck_processing failed: %s", worker_id, _rs_err)

            try:
                entries = dequeue_pending_for_worker(
                    journal_path, batch_size=10,
                    worker_id=worker_id, n_workers=n_workers,
                )
            except Exception as _dq_err:
                logger.warning("reconciler-%d: dequeue failed: %s", worker_id, _dq_err)
                _shutdown_local.wait(0.1)
                continue

            if not entries:
                _empty_dequeues += 1
                _shutdown_local.wait(0.1)
                if _empty_dequeues >= _IDLE_EXIT_EMPTIES and _journal_is_globally_drained(
                    journal_path
                ):
                    logger.info(
                        "reconciler-%d: journal drained after %d idle checks, exiting",
                        worker_id, _empty_dequeues,
                    )
                    break
                continue

            _empty_dequeues = 0

            for entry in entries:
                if _shutdown_local.is_set() or _RECONCILER_SHUTDOWN.is_set():
                    break
                try:
                    materialize_journal_entry(entry, target_base, journal_path)
                except Exception as exc:
                    logger.exception(
                        "reconciler-%d: entry %d (%s) failed: %s",
                        worker_id,
                        entry.get("id"),
                        entry.get("note_id", "?"),
                        exc,
                    )
                _shutdown_local.wait(0.001)
        except Exception as loop_exc:
            logger.error("reconciler-%d loop error: %s", worker_id, loop_exc)
            _shutdown_local.wait(1.0)

    logger.info("reconciler-%d: loop stopped", worker_id)


def multiwriter_reconciliation_pool(
    journal_path: Path,
    target_base: Path,
    n_workers: int = 4,
    idle_quit_after_secs: float = 5.0,
) -> None:
    """Supervise N reconciler worker processes with id-sharded claim.

    Each child runs as an independent subprocess via ``background.fleet_worker``.
    The parent polls the journal to detect when it is quiet and terminates
    workers gracefully.  Used only when ``MEMORY_RECONCILER_N_WORKERS > 1``.

    This avoids the macOS fork-safety issue where daemon threads (connection
    pool revalidator, write queue) are copied into child processes in an
    undefined locked state, causing deadlock.
    """
    if not (n_workers >= 1):
        raise ValueError(f"n_workers must be >= 1, got {n_workers}")

    logger.info(
        "multiwriter pool: spawning n=%d workers (journal=%s)",
        n_workers, journal_path,
    )

    # Build command-line args for each worker
    _worker_cmd_base = [
        sys.executable, "-m", "background.fleet_worker",
        str(journal_path), str(target_base),
    ]

    procs: list[subprocess.Popen] = []
    for k in range(n_workers):
        popen = subprocess.Popen(
            _worker_cmd_base + [str(k), str(n_workers)],
        )
        procs.append(popen)

    idle_start: float | None = None
    _IDLE_SECS = idle_quit_after_secs
    _POLL_INTERVAL = 1.0

    def _journal_is_quiet() -> bool:
        try:
            conn = sqlite3.connect(str(journal_path), timeout=2)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            pending = int(conn.execute(
                "SELECT COUNT(*) AS c FROM write_journal WHERE status='pending'"
            ).fetchone()["c"])
            processing = int(conn.execute(
                "SELECT COUNT(*) AS c FROM write_journal WHERE status='processing'"
            ).fetchone()["c"])
            conn.close()
            return pending == 0 and processing == 0
        except Exception as _jq_exc:
            logger.debug("_journal_is_quiet: %s", _jq_exc)
            return False

    def _terminate_all(timeout: float = 10.0) -> None:
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        deadline = time.time() + timeout
        for p in procs:
            if p.poll() is None and time.time() < deadline:
                try:
                    p.wait(timeout=max(0, deadline - time.time()))
                except Exception:
                    pass
            if p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except Exception:
                    pass

    try:
        while True:
            all_done = True
            for p in procs:
                if p.poll() is None:
                    all_done = False
                    try:
                        p.wait(timeout=_POLL_INTERVAL)
                    except Exception:
                        pass
            if all_done:
                break
            quiet = _journal_is_quiet()
            if quiet:
                if idle_start is None:
                    idle_start = time.time()
                    logger.info("multiwriter pool: journal drained, idle timer started")
                elif time.time() - idle_start >= _IDLE_SECS:
                    logger.info(
                        "multiwriter pool: idle for %.1fs, exiting",
                        time.time() - idle_start,
                    )
                    break
            else:
                if idle_start is not None:
                    logger.debug("multiwriter pool: work detected, resetting idle")
                idle_start = None
    except KeyboardInterrupt:
        logger.warning("multiwriter pool: SIGINT, terminating children")
    except Exception as exc:
        logger.error("multiwriter pool: supervisor error: %s", exc)
    finally:
        _RECONCILER_SHUTDOWN.set()
        _terminate_all(timeout=5)
        for p in procs:
            if p.returncode not in (0, None):
                logger.warning(
                    "multiwriter pool: reconciler-%d exited with code %d",
                    procs.index(p), p.returncode,
                )
    logger.info("multiwriter pool: all workers exited")


def _get_reconciler_worker_count() -> int:
    """Return the configured reconciler worker count (default 1)."""
    try:
        return int(os.environ.get("MEMORY_RECONCILER_N_WORKERS", "1"))
    except (ValueError, TypeError):
        return 1


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


def _resolve_task_timeout(conn: AnyConnection, task_type: str) -> int:
    """Read the per-task-type timeout from cron_task_timeouts (v063+).

    Falls back to MEMORY_WORKER_TASK_TIMEOUT_S env var (default 120s)
    if the table or row doesn't exist.
    """
    try:
        row = conn.execute(
            "SELECT timeout_s FROM cron_task_timeouts WHERE task_type = ?",
            (task_type,),
        ).fetchone()
        if row is not None:
            return int(row[0])
    except Exception:
        pass
    return int(os.environ.get("MEMORY_WORKER_TASK_TIMEOUT_S", "120"))


def _cleanup_task_artifacts(ttype: str, payload: dict) -> None:
    """Clean up subprocess or temp artifacts left by a failed/timed-out task."""
    _temp_dir = payload.get("temp_dir") or payload.get("working_dir")
    if _temp_dir and os.path.isdir(_temp_dir):
        import shutil
        try:
            shutil.rmtree(_temp_dir, ignore_errors=True)
            logger.debug("worker: cleaned up temp dir %s for timed-out task %s", _temp_dir, ttype)
        except Exception as _cln_exc:
            logger.warning("worker: cleanup of %s failed: %s", _temp_dir, _cln_exc)


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

    # Sprint 1.2: Apply task-specific tenant_id if provided in payload
    task_tenant_id = payload.get("tenant_id", "default")
    try:
        conn.create_function("tenant_id", 0, lambda: task_tenant_id)
        conn.execute("DROP VIEW IF EXISTS tenant_memories")
        conn.execute(
            "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
            "SELECT * FROM memories WHERE tenant_id = tenant_id()"
        )
    except Exception:
        pass

    # Resolve cron script paths from CRON_SCRIPT_MAP for cron-style task types.
    if not payload.get("script") and ttype in CRON_SCRIPT_MAP:
        payload = {**payload, "script": CRON_SCRIPT_MAP[ttype]}
        handler = HANDLERS.get("run_script")
    else:
        handler = HANDLERS.get(ttype)

    if not handler:
        fail_task(conn, task_id, f"unknown task type: {ttype}")
        logger.warning("worker: unknown task type %s (id=%d)", ttype, task_id)
        return True

    # Lazy-resolve None entries in HANDLERS (used to break circular imports).
    if handler is None:
        _lazy_map = {
            "entailment_chains": "reasoning.compile.handle_entailment_chains",
            "concept_compilation": "reasoning.compile.handle_concept_compilation",
            "skill_enrichment": "reasoning.compile.handle_skill_enrichment",
            "revalidate_entailments": "reasoning.compile.revalidate_entailment_chains",
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
    #
    # v063: per-task-type timeout is read from cron_task_timeouts table.
    # Fall back to the env var (default 120s) if the table or row is
    # missing.
    import signal as _sig

    _PER_TASK_TIMEOUT_S = _resolve_task_timeout(conn, ttype)

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
    old_handler = None
    _use_signal_timeout = threading.current_thread() is threading.main_thread()

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
        fail_task(conn, task_id, f"timeout after {elapsed:.1f}s")
        logger.error(
            "worker: task %d (%s) TIMED OUT after %.1fs — likely runaway regex or loop",
            task_id,
            ttype,
            elapsed,
        )
        _cleanup_task_artifacts(ttype, payload)
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
        import sqlite3
        from infra.db_path_flock import db_path_flock

        def _open(tenant_id: str = "default"):
            fc = db_path_flock(self._db_path)
            fc.__enter__()
            c = sqlite3.connect(str(self._db_path), timeout=30.0)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            try:
                c.create_function("tenant_id", 0, lambda: tenant_id)
                c.execute(
                    "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
                    "SELECT * FROM memories WHERE tenant_id = tenant_id()"
                )
            except Exception:
                pass
            return c, fc

        processed = 0
        t_drain = time.time()
        last_reset_time = time.time()
        try:
            _DRAIN_MAX_WALL_S = int(
                os.environ.get("MEMORY_WORKER_DRAIN_MAX_WALL_S", "600")
            )
            while not _shutdown:
                # Direct per-task sqlite3 connection (same cross-process
                # serialisation via db_path_flock that sqlite_write_queue
                # provided), so a wedged singleton writer thread can never
                # kill the pool. The lock + RESERVED write lock are
                # released between tasks.
                c, fc = _open()
                try:
                    if time.time() - last_reset_time >= 60.0:
                        try:
                            reset_stuck_processing_tasks(c)
                        except Exception as _reset_exc:
                            logger.debug("worker pool: stuck task reset failed: %s", _reset_exc)
                        last_reset_time = time.time()
                    ok = process_one_task(c, self._db_path, task_type=self._task_type)
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass
                    try:
                        fc.__exit__(None, None, None)
                    except Exception:
                        pass
                if not ok:
                    if drain:
                        break
                    # Idle (no work): the per-task connection is closed, so
                    # the flock/write-lock is released and other writers can
                    # enqueue freely. Just sleep briefly.
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
            logger.info(
                "worker pool: worker %d stopped (processed %d tasks)",
                worker_id, processed,
            )


def _check_high_priority_pending(conn: AnyConnection) -> bool:
    try:
        row = conn.execute("SELECT id FROM task_queue WHERE status = 'pending' LIMIT 1").fetchone()
        return row is not None
    except Exception as _wp_exc:
        logger.warning("_check_high_priority_pending: broad except swallowed: %s", _wp_exc)
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
    # ADD-A-2: signal.signal only works in the main thread.  Guard
    # both handlers so run_worker is safe when called from a test
    # harness or other non-main thread.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    # Short-lived / drain callers don't need LLM extraction.
    # Enforce the extractor-gate here too so tests and other programmatic
    # callers get the same memory savings as the CLI path.
    global _EXTRACTOR_DISABLED
    if drain or once:
        _EXTRACTOR_DISABLED = True
    if _EXTRACTOR_DISABLED:
        logger.info(
            "worker: LLM extractor disabled (drain=%s, once=%s)",
            drain, once,
        )

    logger.info(
        "worker: starting (db=%s, interval=%ds, once=%s, drain=%s, "
        "max_tasks=%d, n_workers=%d)",
        db_path, interval, once, drain, max_tasks, n_workers,
    )

    # Init phase — runs once with a temporary connection, then workers
    # get their own connections via the write queue.
    from infra.db import open_db
    with open_db(db_path, timeout=30.0) as init_conn:
        try:
            init_task_queue(init_conn)
        except Exception as e:
            logger.error("worker: failed to init task queue: %s", e)
            return

        _check_and_reconcile_vec_drift(init_conn, db_path)

        # W1: self-heal partial save state at startup.  A crash between
        # the saga's .md write and the DB commit can leave a forward
        # orphan (.md with no DB row); a crash after commit but before
        # the file write leaves a backward orphan (DB row, missing .md).
        # Reconciling both directions means no partial memory survives a
        # restart of the daemon (the single writer to memory.db).
        # Skipped in drain/once modes: those are short-lived cron tasks
        # that only flush the write journal; orphan reconciliation is
        # a daemon-startup-only concern.
        if not _EXTRACTOR_DISABLED:
            try:
                from memory_integrity import reconcile_orphan_files

                reconcile = reconcile_orphan_files(db_path, db_path.parent)
                n_back = len(reconcile.get("backward_recovered", []))
                n_fwd = len(reconcile.get("forward_reaped", []))
                if n_back or n_fwd:
                    logger.info(
                        "worker: orphan reconciliation healed %d backward / reaped %d forward",
                        n_back,
                        n_fwd,
                    )
            except Exception as _orph_exc:
                logger.debug("worker: orphan reconciliation failed: %s", _orph_exc)

        if not _EXTRACTOR_DISABLED:
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

    # Start the CQRS write-journal reconciliation daemon.
    # TWO modes, selected by ``MEMORY_RECONCILER_N_WORKERS`` env var
    # (default 1 = existing single-thread loop; >1 = multi-process
    # fleet with id-sharded claim for higher throughput):
    #
    #   MEMORY_RECONCILER_N_WORKERS=1  →  _start_reconciler (daemon thread)
    #   MEMORY_RECONCILER_N_WORKERS=4  →  multiwriter_reconciliation_pool (4 OS processes)
    #
    # The task worker pool (``n_workers`` arg) is orthogonal: it controls
    # concurrency of task-queue workers, not journal reconcilers.
    journal_path = db_path.parent / "journal.db"
    reconciler_n_workers = _get_reconciler_worker_count()

    if reconciler_n_workers > 1:
        logger.info(
            "run_worker: multi-process reconciler fleet (n=%d)", reconciler_n_workers
        )
        _RECONCILER_SHUTDOWN.clear()
        multiwriter_reconciliation_pool(
            journal_path, db_path.parent, n_workers=reconciler_n_workers
        )
        # Pool exits cleanly when journal drains or on signal — return.
        return

    # Default: single-reconciler thread (behaviour bitidentical to before).
    reconciler_thread = _start_reconciler(journal_path, db_path.parent)

    # Delegate to WorkerPool when n_workers > 1
    if n_workers > 1:
        pool = WorkerPool(db_path, n_workers=n_workers, task_type=task_type)
        pool.run(drain=drain, max_tasks=max_tasks)
        _RECONCILER_SHUTDOWN.set()
        reconciler_thread.join(timeout=5)
        return

    # Single-threaded path
    #
    # Robustness note (2026-07-16): this path previously opened ONE
    # ``sqlite_write_queue.start_session`` for the whole process. That
    # session is bounded to MEMORY_WRITE_QUEUE_MAX_S (300s) and
    # force-rolls-back past that, which silently killed persistent
    # (launchd) workers after 5 minutes. Worse, the queue's singleton
    # writer thread becomes wedged once a session force-closes, so every
    # subsequent start_session timed out and raised — taking the whole
    # worker down with it.
    #
    # The worker IS the single writer (Rule 13: single-writer on main
    # DB). So instead of routing through the fragile proxy+writer-thread,
    # we open a direct sqlite3 connection PER TASK, wrapped in the
    # same per-DB-path ``db_path_flock`` cross-process serialisation the
    # queue provided. No singleton thread to wedge, no 300s ceiling, and
    # the RESERVED write lock + flock are released between tasks so other
    # writers (health check, cron) can enqueue while this worker polls.
    import sqlite3
    from infra.db_path_flock import db_path_flock

    def _open_task_conn(tenant_id: str = "default"):
        flock_ctx = db_path_flock(db_path)
        flock_ctx.__enter__()
        c = sqlite3.connect(str(db_path), timeout=30.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        # Match connection_pool.get() tenant isolation primitives.
        try:
            c.create_function("tenant_id", 0, lambda: tenant_id)
            c.execute(
                "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
                "SELECT * FROM memories WHERE tenant_id = tenant_id()"
            )
        except Exception:
            pass
        return c, flock_ctx

    last_reset_time = time.time()
    try:
        # Process-level safety timeout: kill the entire process if any
        # mode (drain, once, interval) runs longer than this cap.
        # Prevents zombie workers from holding the flock lock forever.
        _PROCESS_TIMEOUT_S = int(
            os.environ.get("MEMORY_WORKER_PROCESS_TIMEOUT_S", "3600")
        )
        import signal as _proc_sig

        def _process_killer(signum, frame):
            logger.error(
                "worker: process exceeded %ds timeout — force-exiting",
                _PROCESS_TIMEOUT_S,
            )
            os._exit(1)

        _use_proc_signal = threading.current_thread() is threading.main_thread()
        if _use_proc_signal:
            _proc_sig.signal(_proc_sig.SIGALRM, _process_killer)
        _proc_sig.alarm(_PROCESS_TIMEOUT_S)

        def _process_one():
            c, flock_ctx = _open_task_conn()
            try:
                return process_one_task(c, db_path, task_type=task_type)
            finally:
                try:
                    c.close()
                except Exception:
                    pass
                try:
                    flock_ctx.__exit__(None, None, None)
                except Exception:
                    pass

        if drain:
            _DRAIN_MAX_WALL_S = int(
                os.environ.get("MEMORY_WORKER_DRAIN_MAX_WALL_S", "600")
            )
            processed = 0
            t_drain = time.time()
            while not _shutdown and processed < max_tasks:
                if time.time() - last_reset_time >= 60.0:
                    try:
                        c, flock_ctx = _open_task_conn()
                        try:
                            reset_stuck_processing_tasks(c)
                        finally:
                            c.close()
                            flock_ctx.__exit__(None, None, None)
                    except Exception as _reset_exc:
                        logger.debug("worker: stuck task reset failed: %s", _reset_exc)
                    last_reset_time = time.time()
                if time.time() - t_drain > _DRAIN_MAX_WALL_S:
                    logger.warning(
                        "worker: drain hit wall-clock cap of %ds after %d tasks — exiting",
                        _DRAIN_MAX_WALL_S,
                        processed,
                    )
                    break
                ok = _process_one()
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
            elapsed = time.time() - t_drain
            logger.info(
                "worker: drain complete — processed %d tasks in %.1fs (%.1f tasks/sec)",
                processed,
                elapsed,
                processed / elapsed if elapsed > 0 else 0,
            )
            _proc_sig.alarm(0)
            return
        else:
            batch_size = _get_effective_batch_size()
            while not _shutdown:
                if time.time() - last_reset_time >= 60.0:
                    try:
                        c, flock_ctx = _open_task_conn()
                        try:
                            reset_stuck_processing_tasks(c)
                        finally:
                            c.close()
                            flock_ctx.__exit__(None, None, None)
                    except Exception as _reset_exc:
                        logger.debug("worker: stuck task reset failed: %s", _reset_exc)
                    last_reset_time = time.time()
                batch_processed = 0
                while batch_processed < batch_size:
                    ok = _process_one()
                    if not ok:
                        break
                    batch_processed += 1
                if once:
                    break
                if batch_processed == 0:
                    for _ in range(interval):
                        if _shutdown:
                            break
                        try:
                            c, flock_ctx = _open_task_conn()
                            try:
                                if _check_high_priority_pending(c):
                                    break
                            finally:
                                c.close()
                                flock_ctx.__exit__(None, None, None)
                        except Exception as _hp_exc:
                            logger.debug("worker: hp probe failed: %s", _hp_exc)
                        time.sleep(1)
    finally:
        if _use_proc_signal:
            _proc_sig.alarm(0)
        _RECONCILER_SHUTDOWN.set()
        try:
            reconciler_thread.join(timeout=5)
        except NameError:
            pass
        except Exception as _wp_exc:
            logger.warning("run_worker: broad except swallowed: %s", _wp_exc)
            pass
        logger.info("worker: stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    # Step 8 (cron-pipeline-no-flock): when the process-singleton lock is
    # skipped, overlapping workers are observable + recoverable via the
    # pipeline-coverage health check (cron_pipeline_health.py) instead of
    # being hard-gated by flock. Default behaviour keeps the lock.
    _no_flock = os.environ.get("MEMORY_CRON_NO_FLOCK", "") == "1"

    # H-fix (2026-06-22): acquire flock BEFORE arg parsing so two
    # cron ticks that fire 5 minutes apart don't both run the worker
    # concurrently. Without this, --drain mode would accumulate one
    # worker per cron tick (32 workers seen in 90 minutes on a busy
    # system, all racing on the same SQLite WAL).
    if _no_flock:
        logger.info(
            "background_worker: MEMORY_CRON_NO_FLOCK=1 — skipping process "
            "singleton lock; overlaps observable via pipeline-coverage"
        )
    else:
        try:
            from cron._flock import acquire_lock_or_exit

            acquire_lock_or_exit("background_worker")
        except ImportError:
            # Best-effort: if flock module isn't on path, fall back to
            # a lightweight inline lock using fcntl directly.
            # NOTE: do NOT re-import `from pathlib import Path` here —
            # the import at module scope (line 43) already makes Path
            # available. A local import makes `Path` a function-local
            # name in the entire main(), which would then raise
            # UnboundLocalError on the try-success path (when the cron
            # flock module IS importable) at `Path(args.db)` on line 612+.
            import fcntl

            _lock_path = (
                Path.home()
                / ".config"
                / "agentic-memory"
                / "memory"
                / "locks"
                / "background_worker.lock"
            )
            _lock_path.parent.mkdir(parents=True, exist_ok=True)
            _lock_fd = open(_lock_path, "w")
            try:
                fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info("background_worker: another instance holds the lock, exiting")
                _lock_fd.close()
                return 0
            # Hold the fd alive for the lifetime of the worker
            global _BACKGROUND_WORKER_LOCK_FD
            _BACKGROUND_WORKER_LOCK_FD = _lock_fd

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
    parser.add_argument(
        "--no-extractor",
        action="store_true",
        help="Disable LLM fact extractor (saves ~2 GB RAM on startup). "
        "Automatically implied for --drain, --once, and --max-tasks modes. "
        "Also set via MEMORY_LLM_EXTRACTION=0 env var.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    # ------------------------------------------------------------------ #
    # LLM extractor gate                                                    #
    # Drain/once/short-lived workers only flush the write journal and do   #
    # not need LLM fact extraction (saves ~2 GB mmap + torch/tokenizers).  #
    # Also honour the explicit --no-extractor flag and the                  #
    # MEMORY_LLM_EXTRACTION=0 env var (same flag used by the save path).   #
    # ------------------------------------------------------------------ #
    global _EXTRACTOR_DISABLED
    if args.no_extractor:
        _EXTRACTOR_DISABLED = True
    elif args.drain or args.once or args.max_tasks is not None:
        _EXTRACTOR_DISABLED = True
    elif os.environ.get("MEMORY_LLM_EXTRACTION", "").strip().lower() in ("0", "false", "no", "off"):
        _EXTRACTOR_DISABLED = True
    if _EXTRACTOR_DISABLED:
        logger.info(
            "worker: LLM extractor disabled (drain=%s, once=%s, max_tasks=%s, no_extractor=%s)",
            args.drain, args.once, args.max_tasks is not None, args.no_extractor,
        )

    if args.db:
        db_path = Path(args.db)
    else:
        env = os.environ.get("MEMORY_DB_PATH")
        if env:
            db_path = Path(env)
        else:
            db_path = resolve_active_memory_dir() / "memory.db"

    interval = args.interval or int(os.environ.get("MEMORY_WORKER_INTERVAL", "300"))

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
