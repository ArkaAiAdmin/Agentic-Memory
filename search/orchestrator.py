from __future__ import annotations

"""12-phase hybrid search orchestrator for agentic-memory.

Pipeline phases (executed in order):
  Phase 0  — Input normalization & query type detection
  Phase 1  — FTS5 BM25 retrieval
  Phase 2  — Vector (usearch) retrieval
  Phase 3  — ColBERT late-interaction retrieval
  Phase 4  — Reciprocal Rank Fusion (RRF) merge
  Phase 5  — Cross-encoder reranking (optional)
  Phase 6  — Temporal decay application
  Phase 7  — Neural forget curve adjustment
  Phase 8  — KG concept/centrality boost
  Phase 9  — Final score computation & ranking
  Phase 10 — Result envelope construction
  Phase 11 — Error counter & latency logging

Error handling: each phase is individually isolated. On failure, the
phase increments its error counter (via ``infra.error_counter``) and
the pipeline falls through to the next phase with degraded results.
No single phase failure kills the search.

Thread safety: uses module-level ``_db_columns_cache`` (RLock) and
``_phase_latencies`` (RLock) for cross-call shared state.
"""

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

from infra.cache import (
    _search_cache,
    _search_cache_lock,
    SEARCH_CACHE_MAX,
    SEARCH_CACHE_TTL,
    SEARCH_CACHE_TTL_ENABLED,
    make_cache_key,
)
from infra.memory_common import (
    connection_pool,
    safe_close_db,
)
from infra.infrastructure import (
    _err,
    ErrorCode,
)
from infra.error_counter import increment as _phase_inc, get_counts as _phase_counts, reset as _phase_reset
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

# Import functions from other search submodules
from search.query_parser import (
    _parse_search_query,
    _build_zero_result_suggestions,
    _detect_query_type,
    _weights_for_query_type,
)
from search.rerankers import (
    _apply_single_ce_rerank,
    _apply_late_interaction_rerank,
    _select_ce_mode,
)
from search.scoring import (
    _reciprocal_rank_fusion,
    _strong_match_float,
    _compute_final_score,
    _normalize_bm25_ranks,
    compute_channel_weights,
)
from search.enrichment import _apply_post_rank_metadata
from search.synthesis import (
    _bb1_synthesize,
)

# Docstrings for imported search functions (defined in search.query_parser /
# search.scoring but exposed here as part of the orchestrator's public surface).
_parse_search_query.__doc__ = (
    """Normalize and tokenize a raw search query.

    Args:
        query: Raw natural-language query string from the caller.
        db_path: Path to the SQLite DB (used for synonym expansion).

    Returns:
        A 4-tuple ``(normalized_query, fts_query, bare_text,
        graph_rag_terms)`` where ``normalized_query`` is the
        Unicode-normalized lowercase form, ``fts_query`` is
        FTS5-safe escaped query, ``bare_text`` is the raw extracted
        text, and ``graph_rag_terms`` are tokens for KG expansion.
    """
)
_reciprocal_rank_fusion.__doc__ = (
    """Fuse multiple ranked result lists via Reciprocal Rank Fusion (RRF).

    Args:
        ranked_lists: Iterable of lists, each a ranked sequence of
            doc_ids (or (doc_id, score) tuples) ordered by
            descending relevance.
        k: RRF dampening constant (default 60).
        weights: Optional per-list weight multipliers.  Must be the
            same length as ``ranked_lists``.  ``None`` gives equal
            weight to all lists.

    Returns:
        A ``dict`` mapping ``doc_id`` → float RRF score.  Documents
        appearing in multiple lists receive summed weighted scores.
    """
)
_compute_final_score.__doc__ = (
    """Compute the weighted final score for a single search result.

    Combines five retrieval channels into a single float:
        bm25 (0.45), fitness (0.25), importance (0.15),
        pinned (0.10), tag_match (0.05).

    Weights are loaded from config ``rerank_weights`` JSON if set,
    otherwise the defaults are used.  Temporal decay / forgetting
    curve is applied by callers AFTER this step.

    Args:
        ctx: A ``ScoreContext`` named-tuple carrying the per-result
            attributes (``rank``, ``fitness``, ``importance``,
            ``pinned``, ``created``, ``tags_json``, ``query``,
            ``boost_pinned``, ``recency_weight``, ``weights``,
            ``now_ts``).

    Returns:
        A float in [0, ~1.5] representing the combined relevance
        score for this result.
    """
)

logger = logging.getLogger(__name__)

_db_columns_cache: dict = {}
_db_columns_cache_lock = threading.Lock()

# Backward-compatible phase latency tracking (pre-error_counter API).
_phase_latencies: dict[str, float] = {}
_phase_latencies_lock = threading.Lock()


def _get_memories_columns(db: AnyConnection) -> set[str]:
    """Cache memories table columns by DB path to save repeated PRAGMA queries.

    Thread-safe: protected by ``_db_columns_cache_lock``.  Returns a
    ``set`` of column name strings for the ``memories`` table,
    populating the module-level cache on first call per DB path.

    Args:
        db: Active ``sqlite3.Connection`` (or AnyConnection wrapper)
            used to execute ``PRAGMA database_list`` and
            ``PRAGMA table_info(memories)``.

    Returns:
        A ``set[str]`` of column names in the ``memories`` table.
        Returns an empty ``set`` on any ``sqlite3.Error``.
    """
    try:
        db_path_row = db.execute("PRAGMA database_list").fetchone()
        db_path = db_path_row[2] if db_path_row is not None and len(db_path_row) > 2 else ""
    except sqlite3.Error:
        db_path = ""

    if not db_path:
        try:
            return {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall() if len(row) > 1
            }
        except sqlite3.Error:
            return set()

    with _db_columns_cache_lock:
        cols = _db_columns_cache.get(db_path)

    if cols is None:
        try:
            cols = {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall() if len(row) > 1
            }
            with _db_columns_cache_lock:
                _db_columns_cache[db_path] = cols
        except sqlite3.Error:
            cols = set()

    return cols


# Only allow safe SQL fragments in extra_filter to prevent injection.
# Safe characters: spaces, alphanumeric, SQL punctuation (AND/OR/NOT/=, etc.)
# Ban semicolons to prevent multi-statement injection.
_SQL_SAFE_FILTER_RE = re.compile(r"^[ A-Za-z0-9_.,=<>!()'\"%\-/?]+$")
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_sql_columns(columns: str) -> bool:
    """Validate that a comma-separated column list contains only safe identifiers."""
    for col in columns.split(","):
        col = col.strip().split(" AS ")[0].strip()  # strip alias
        if not _SQL_IDENT_RE.match(col):
            return False
    return True


def _fetch_rows_by_ids(
    db: AnyConnection,
    ids: list,
    table: str = "tenant_memories",
    columns: str = "id, content, source_file, tags, created_at, fitness_score, importance, pinned, last_accessed, metadata, access_count",
    extra_filter: str = "",
    extra_params: tuple = (),
) -> dict:
    """Batch-fetch rows by IDs to avoid N+1 queries. Returns {id: row_tuple}.

    Defaults to the ``tenant_memories`` TEMP VIEW so results are scoped to the
    current tenant (the connection's ``tenant_id()`` function). Pass
    ``table="memories"`` explicitly for administrative cross-tenant reads.

    Chunks at 500 IDs per query to stay under SQLite's ~999 variable limit.
    """
    if not ids:
        return {}
    if not _validate_sql_columns(columns):
        logger.warning("_fetch_rows_by_ids: rejecting unsafe columns=%r", columns)
        return {}
    if not _SQL_IDENT_RE.match(table.split()[0]):
        logger.warning("_fetch_rows_by_ids: rejecting unsafe table=%r", table)
        return {}
    if extra_filter and not _SQL_SAFE_FILTER_RE.match(extra_filter):
        logger.warning(
            "_fetch_rows_by_ids: rejecting unsafe extra_filter=%r", extra_filter
        )
        return {}
    result = {}
    _CHUNK_SIZE = 500
    for i in range(0, len(ids), _CHUNK_SIZE):
        chunk = ids[i : i + _CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        query = f"SELECT {columns} FROM {table} m WHERE m.id IN ({placeholders}) AND m.deleted_at IS NULL{extra_filter}"
        try:
            rows = db.execute(query, [*chunk, *extra_params]).fetchall()
            result.update({row[0]: row for row in rows})
        except sqlite3.Error:
            logger.warning("_fetch_rows_by_ids: chunk of %d ids failed", len(chunk))
            continue
    return result


def _merge_chunk_hits(chunk_hits: list) -> list:
    """Merge consecutive chunk hits from the same parent into a single result.

    Returns a list of (parent_id, best_chunk_idx, combined_text,
    combined_rank, chunk_count) tuples. Consecutive chunks from the same
    parent are merged; non-consecutive chunks from the same parent keep
    the best-scoring one.
    """
    if not chunk_hits:
        return []
    by_parent: dict = {}
    for hit in chunk_hits:
        parent_id = hit[0]
        if parent_id not in by_parent:
            by_parent[parent_id] = []
        by_parent[parent_id].append(hit)
    merged = []
    for parent_id, hits in by_parent.items():
        hits.sort(key=lambda h: h[1])
        groups = []
        current_group = [hits[0]]
        for h in hits[1:]:
            prev_idx = current_group[-1][1]
            if h[1] == prev_idx + 1:
                current_group.append(h)
            else:
                groups.append(current_group)
                current_group = [h]
        groups.append(current_group)
        for group in groups:
            best = min(group, key=lambda h: h[5])
            combined_text = " ".join((h[2] for h in group))
            combined_rank = min((h[5] for h in group))
            merged.append(
                (parent_id, best[1], combined_text, combined_rank, len(group))
            )
    merged.sort(key=lambda x: x[3])
    return merged


def _search_chunks_enhanced(db: AnyConnection, fts_query: str, limit: int) -> list:
    """Enhanced chunk search that returns parent_id metadata directly.

    Returns (parent_id, chunk_idx, chunk_text, start_offset, end_offset,
    rank) tuples, same as _qw5_search_chunks but with parent_id included
    in the FTS index for faster merging.
    """
    try:
        rows = db.execute(
            "SELECT mc.parent_id, mc.chunk_idx, mc.content,\n                      mc.start_offset, mc.end_offset, fts.rank\n               FROM memory_chunks_fts fts\n               JOIN memory_chunks mc ON mc.id = fts.rowid\n               WHERE memory_chunks_fts MATCH ?\n               ORDER BY fts.rank\n               LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return rows


@dataclass
class ScoreContext:
    rank: float
    fitness: Optional[float]
    importance: Optional[int]
    pinned: Optional[bool]
    created: Optional[str]
    tags_json: Optional[str]
    query: str
    boost_pinned: bool
    recency_weight: float
    now_ts: Optional[float] = None
    weights: Optional[dict] = None
    is_entailed: Optional[int] = None


class MemoryResultRow(NamedTuple):
    """Named columns for a search result row."""

    id: str
    content: str
    source_file: str
    tags: str
    created: str
    rank: float
    final_score: float
    fitness: float
    importance: int
    pinned: int
    last_accessed: Optional[str] = None
    metadata: Optional[str] = None


def _resolve_late_interaction_enabled() -> bool:
    """Eagerly resolve the late-interaction flag from config."""
    try:
        from infra._lazy_imports import get_config

        return bool(getattr(get_config(), "late_interaction", True))
    except (ImportError, AttributeError):
        return True


def _get_embedding_score_threshold() -> float:
    try:
        from infra._lazy_imports import get_config

        return float(get_config().embedding_score_threshold)
    except (ImportError, AttributeError):
        return 0.25


# Cache for skill-first lookups to prevent double-incrementing hit_count
# Bounded LRU via OrderedDict; evicts oldest entries past MAX_SKILL_CACHE.
# Thread-safe: all cache reads and writes are protected by _skill_cache_lock.
_SKILL_CACHE_MAX = 512
_skill_cache: dict[tuple[str, tuple[str, ...], int], dict] = {}
_skill_cache_order: list[tuple[str, tuple[str, ...], int]] = []
_skill_cache_lock = threading.Lock()


def _skill_first_lookup(db_path: Path, terms: list[str], limit: int, tenant_id: str = "default") -> dict | None:
    """Look up skills in memory_skills table matching the query terms."""
    cache_key = (str(db_path), tuple(sorted(terms)), limit)
    with _skill_cache_lock:
        if cache_key in _skill_cache:
            return _skill_cache[cache_key]

    try:
        from infra._lazy_imports import connection_pool, safe_close_db

        db = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
    except sqlite3.Error as exc:
        logger.warning("_skill_first_lookup: connection_pool.get failed: %s", exc)
        return None
    try:
        # Check if memory_skills table exists
        try:
            table_check = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_skills'"
            ).fetchone()
        except sqlite3.Error:
            return None
        if not table_check:
            return None

        # Search for skills matching any of the terms
        like_clauses = []
        params = []
        for term in terms:
            like_clauses.append(
                "(name LIKE ? OR topic LIKE ? OR description LIKE ? OR triggers LIKE ?)"
            )
            wild = f"%{term}%"
            params.extend([wild, wild, wild, wild])

        where = " OR ".join(like_clauses)
        try:
            rows = db.execute(
                f"SELECT id, name, source_memory_id, topic, description, triggers, steps, hit_count "
                f"FROM memory_skills WHERE {where} "
                f"ORDER BY hit_count DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        except sqlite3.Error:
            return None

        if not rows:
            return None

        # Batch increment hit_count for matched skills (H3 fix: single UPDATE with IN clause)
        now_ts = time.time()
        skill_ids = [row[0] for row in rows]
        placeholders = ",".join("?" * len(skill_ids))
        try:
            db.execute(
                f"UPDATE memory_skills SET hit_count = hit_count + 1, last_used_at = ? WHERE id IN ({placeholders})",
                [now_ts] + skill_ids,
            )
            db.commit()
        except sqlite3.Error as e:
            logger.warning("_skill_first_lookup: hit_count update failed: %s", e)

        # Format results
        results = []
        for row in rows:
            results.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "source_memory_id": row[2],
                    "topic": row[3],
                    "description": row[4],
                    "is_skill": True,
                    "score": 1.0,
                }
            )

        # Build output text
        output_lines = [f"Skill match: {row[1]}" for row in rows]
        output = "\n".join(output_lines)

        result = {
            "results": results,
            "count": len(results),
            "output": output,
        }
        with _skill_cache_lock:
            _skill_cache[cache_key] = result
            _skill_cache_order.append(cache_key)
            if len(_skill_cache) > _SKILL_CACHE_MAX:
                _oldest = _skill_cache_order.pop(0)
                _skill_cache.pop(_oldest, None)
        return result
    finally:
        try:
            safe_close_db(db)
        except Exception as e:
            logger.warning("_skill_first_lookup: safe_close_db failed: %s", e)


# Map the legacy ``memory_record_ctr_feedback`` action vocabulary onto the
# unified ``memory_search_interaction`` action vocabulary.  Each legacy action
# becomes its OWN row (keyed by (query_id, memory_id, action)), so a
# returned+clicked pair for the same query/memory no longer collapses into a
# single row the way ``INSERT OR REPLACE`` on ``memory_ctr_feedback`` did.
_CTR_FEEDBACK_ACTION_MAP = {
    "returned": "impression",
    "clicked": "click",
    "dismissed": "dismissed",
}


def record_ctr_feedback_db(
    db_path: str | Path,
    id: str,
    query_id: str,
    action: str = "returned",
    returned_at: Optional[float] = None,
    source: Optional[str] = None,
    ranking_params: Optional[str] = None,
    tenant_id: str = "default",
) -> None:
    """Record CTR feedback as an ``memory_search_interaction`` row.

    Phase 0 (audit #9) fix: previously this wrote to ``memory_ctr_feedback``
    with ``INSERT OR REPLACE``, which collapsed multi-event rows — a second
    ``returned`` for an already-clicked (query_id, memory_id) pair would
    ``DELETE``+re-``INSERT`` and wipe the ``clicked_at`` / ``dismissed_at``
    columns.  We now write one row per (query_id, memory_id, action) into
    ``memory_search_interaction`` using ``ON CONFLICT(query_id, memory_id,
    action) DO UPDATE`` so re-recording only refreshes ``ts``/``rank`` and
    never destroys the sibling action rows.

    Args:
        db_path: Path to the memory SQLite database.
        id: Memory id (the legacy ``memory_ctr_feedback.id`` column maps to
            ``memory_search_interaction.memory_id``).
        query_id: Search query correlation id.
        action: One of ``returned`` / ``clicked`` / ``dismissed`` (legacy) or
            any ``memory_search_interaction`` action string.
        returned_at: Optional explicit timestamp (epoch seconds).
        source: Legacy column, no longer stored (kept for signature compat).
        ranking_params: Legacy column, no longer stored (kept for compat).
        tenant_id: Tenant namespace for multi-tenant isolation.
    """
    mapped_action = _CTR_FEEDBACK_ACTION_MAP.get(action, action)
    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        conn.execute(
            "INSERT INTO memory_search_interaction "
            "(query_id, memory_id, action, tenant_id, rank) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(query_id, memory_id, action) "
            "DO UPDATE SET ts=excluded.ts, rank=excluded.rank",
            (query_id, id, mapped_action, tenant_id, None),
        )
        conn.commit()
    finally:
        safe_close_db(conn)


def record_memory_used_in_response(
    db_path: str | Path,
    query_id: str,
    memory_ids: list[str],
    tenant_id: str = "default",
    ranks: Optional[list[int]] = None,
) -> None:
    """Record that the given memories were actually presented to the
    user/agent for ``query_id`` (the ``used_in_response`` CTR signal).

    One row per (query_id, memory_id) is written to
    ``memory_search_interaction`` with ``action='used_in_response'``.  Uses
    ``ON CONFLICT(query_id, memory_id, action) DO UPDATE`` so re-recording the
    same pair only bumps ``ts``/``rank`` (never collapses multi-event rows).

    This is the producer counterpart to ``record_ctr_feedback_db``: it fires
    when a recalled memory is surfaced in a response (e.g. the session-start
    recap injected into the system prompt), which is a stronger signal than a
    mere search impression.

    Args:
        db_path: Path to the memory SQLite database.
        query_id: Search query correlation id.
        memory_ids: Memory ids that were shown.
        tenant_id: Tenant namespace for multi-tenant isolation.
        ranks: Optional 1-based display ranks aligned to ``memory_ids``.
    """
    if not memory_ids:
        return
    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        for i, memory_id in enumerate(memory_ids):
            rank = ranks[i] if (ranks is not None and i < len(ranks)) else None
            conn.execute(
                "INSERT INTO memory_search_interaction "
                "(query_id, memory_id, action, tenant_id, rank) "
                "VALUES (?, ?, 'used_in_response', ?, ?) "
                "ON CONFLICT(query_id, memory_id, action) "
                "DO UPDATE SET ts=excluded.ts, rank=excluded.rank",
                (query_id, memory_id, tenant_id, rank),
            )
        conn.commit()
    finally:
        safe_close_db(conn)


def _record_drift_event(
    conn: AnyConnection,
    centroid: Any,
    diff: Any,
    drift: float,
    threshold: float,
    *,
    is_baseline: bool = False,
    min_seconds_between_writes: float = 60.0,
    time_mod=None,
) -> tuple[str, int, bool]:
    """Write a single concept-drift event to ``concept_drift`` + ``drift_alarms``.

    Shared between ``check_concept_drift_db`` (the MCP tool path) and
    ``cron/cron_concept_drift.py`` (the scheduled path).  E2 / G8 fix
    (2026-06-22): the write logic used to be duplicated in both
    call sites and could write duplicate rows in the same time
    window.  The dedupe check (``min_seconds_between_writes``) makes
    a re-run within ``min_seconds_between_writes`` of the previous
    write a no-op — the cron + MCP tool can run back-to-back without
    polluting the table.

    Args:
        conn: Open ``sqlite3.Connection``.  Caller manages commit.
        centroid: numpy array of the current embedding centroid.
        diff: numpy array of ``centroid - prev_centroid`` (or
            ``centroid`` if no prior).  Used to compute per-memory
            alarm contributions on the top-5 dimensions.
        drift: Cosine distance between current and previous centroid.
            When ``drift < threshold`` and ``is_baseline`` is False,
            this function is a no-op.
        threshold: Cosine-distance threshold (used both for the gate
            and to record the threshold snapshot in the alarm row).
        is_baseline: When True, force a write even if ``drift == 0``
            (first run after a fresh DB).  The alarm_level for a
            baseline event is forced to ``info``.
        min_seconds_between_writes: Skip the write if the most recent
            ``concept_drift`` row is younger than this.  G8 fix.
        time_mod: Optional module with ``time()`` / ``gmtime()`` /
            ``strftime``.  Defaults to the standard library ``time``.
            Cron / tests can pass a fake.

    Returns:
        ``(alarm_id, n_alarms_written, was_written)``.  ``was_written``
        is False when the gate (``drift >= threshold`` or
        ``is_baseline``) is not met, or when the dedupe window
        suppresses the write.  The caller should still commit on a
        no-op (no harm; the read path doesn't care).
    """
    import time as _time

    if time_mod is None:
        time_mod = _time

    if drift < threshold and not is_baseline:
        return "", 0, False

    # G8 fix: skip if a row was written very recently. Without this
    # gate, the MCP tool + cron running back-to-back would write
    # duplicate rows with the same drift_metric.
    try:
        recent = conn.execute(
            "SELECT triggered_at FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
        ).fetchone()
        if recent and recent[0] is not None:
            try:
                age = float(time_mod.time()) - float(recent[0])
                if age < min_seconds_between_writes:
                    return "", 0, False
            except (TypeError, ValueError):
                pass
    except sqlite3.OperationalError:
        # concept_drift table doesn't exist yet — first run; allow write.
        pass

    import numpy as _np

    alarm_id = f"drift_{int(time_mod.time())}"
    detected_at_iso = time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", time_mod.gmtime())
    # The `drifted_dimensions` column stores the centroid (not a list
    # of dim deltas).  Downstream read code parses it as a numpy
    # centroid; storing the dim delta list there breaks the read+write
    # pair.  The dim-delta summary lives in the per-memory alarm
    # `concept` field instead.
    centroid_json = json.dumps(centroid.tolist())
    conn.execute(
        "INSERT INTO concept_drift "
        "(id, drift_metric, drifted_dimensions, triggered_at) "
        "VALUES (?, ?, ?, ?)",
        (alarm_id, round(drift, 4), centroid_json, time_mod.time()),
    )

    # Severity tier for the per-memory alarms.
    if is_baseline:
        alarm_level = "info"
    elif drift >= 2.0 * threshold:
        alarm_level = "critical"
    elif drift >= 1.5 * threshold:
        alarm_level = "warning"
    else:
        alarm_level = "info"

    # Top-5 dimensions that drifted the most.
    top_idxs = sorted(
        range(len(diff)),
        key=lambda i: -abs(float(diff[i])),
    )[:5]
    n_alarms_written = 0
    try:
        top_memory_rows = conn.execute(
            "SELECT memory_id, embedding FROM memory_embeddings"
        ).fetchall()
        scored = []
        for mem_id, blob in top_memory_rows:
            if not blob:
                continue
            try:
                vec = _np.frombuffer(blob, dtype=_np.float32).copy()
            except (ValueError, BufferError):
                continue
            contrib = float(
                sum(abs(float(vec[i])) * abs(float(diff[i])) for i in top_idxs)
            )
            scored.append((mem_id, contrib))
        scored.sort(key=lambda x: -x[1])
        # Cap per-event fan-out: 10 alarms per drift event. This
        # keeps the table queryable even during severe drift bursts;
        # the operator can re-run for more if needed.
        for mem_id, _ in scored[:10]:
            try:
                conn.execute(
                    "INSERT INTO drift_alarms "
                    "(memory_id, concept, drift_score, threshold, "
                    " alarm_level, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        mem_id,
                        f"embedding_dim_top{','.join(str(i) for i in top_idxs)}",
                        round(drift, 4),
                        threshold,
                        alarm_level,
                        detected_at_iso,
                    ),
                )
                n_alarms_written += 1
            except sqlite3.IntegrityError:
                # FK violation (memory hard-deleted between read and
                # write) is non-fatal; skip.
                continue
    except sqlite3.OperationalError:
        # drift_alarms table doesn't exist yet (pre-v15 DB); silently
        # skip per-memory alarms.
        pass

    return alarm_id, n_alarms_written, True


def check_concept_drift_db(db_path: str | Path, threshold: float = 0.15, tenant_id: str = "default") -> dict:
    """Check concept drift with connection lifecycle managed.

    Writes a row to the ``concept_drift`` table when drift exceeds the
    threshold. Also writes a per-memory alarm to ``drift_alarms`` (added
    in v15) for every memory whose top-drifted-dimension index
    corresponds to a high-contribution row, so operators have a
    per-memory view of which notes triggered the alarm.

    E2 / G8 fix (2026-06-22): the actual write logic now lives in
    ``_record_drift_event`` so the cron and MCP paths share one
    implementation.  ``_record_drift_event`` also enforces a 60-second
    dedupe window so back-to-back invocations don't write duplicate
    rows to the ``concept_drift`` table.
    """
    import numpy as _np

    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        rows = conn.execute("SELECT embedding FROM memory_embeddings").fetchall()
        if not rows:
            return {
                "drift_metric": 0.0,
                "drifted_dimensions": [],
                "alarm_id": "",
                "n_embedded": 0,
                "note": "no embeddings found",
            }
        vectors = []
        for (blob,) in rows:
            vec = _np.frombuffer(blob, dtype=_np.float32).copy()
            vectors.append(vec)
        if not vectors:
            return {
                "drift_metric": 0.0,
                "drifted_dimensions": [],
                "alarm_id": "",
                "n_embedded": 0,
                "note": "no embeddings found",
            }
        from collections import Counter
        dims = [len(v) for v in vectors]
        most_common_dim = Counter(dims).most_common(1)[0][0]
        vectors = [v for v in vectors if len(v) == most_common_dim]
        embeddings = _np.stack(vectors)
        centroid = embeddings.mean(axis=0)
        prev = conn.execute(
            "SELECT drifted_dimensions FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
        ).fetchone()
        prev_centroid = None
        if prev and prev[0]:
            try:
                prev_centroid = _np.array(json.loads(prev[0]))
            except json.JSONDecodeError as _de:
                logger.warning("concept_drift: failed to parse drifted_dimensions: %s", _de)
        if prev_centroid is not None and len(prev_centroid) == len(centroid):
            cos_sim = float(
                _np.dot(centroid, prev_centroid)
                / (_np.linalg.norm(centroid) * _np.linalg.norm(prev_centroid) + 1e-10)
            )
            drift = 1.0 - cos_sim
            diff = centroid - prev_centroid
        else:
            drift = 0.0
            diff = centroid
        top_dims = sorted(
            enumerate(abs(diff).tolist()),
            key=lambda x: -x[1],
        )[:5]
        drifted = [
            {"index": idx, "delta": round(float(diff[idx]), 4)} for idx, _ in top_dims
        ]
        # E2 fix (2026-06-22): detect "first run / no prior centroid" and
        # pass ``is_baseline=True`` so the shared writer records a
        # baseline row + per-memory alarm.  Without this, the orchestrator
        # would never write on the first run (drift is always 0 when
        # prev_centroid is None), making ``concept_drift`` look empty
        # until something actually drifts.  The cron path already
        # detected this case; this brings the MCP path into parity.
        is_baseline = prev_centroid is None
        alarm_id, n_alarms_written, was_written = _record_drift_event(
            conn,
            centroid,
            diff,
            drift,
            threshold,
            is_baseline=is_baseline,
        )
        if was_written:
            conn.commit()
        return {
            "drift_metric": round(drift, 4),
            "drifted_dimensions": drifted,
            "alarm_id": alarm_id,
            "n_embedded": len(vectors),
            "n_alarms_written": n_alarms_written,
            "alarm_level": (
                "critical"
                if drift >= 2.0 * threshold
                else "warning"
                if drift >= 1.5 * threshold
                else "info"
                if drift >= threshold
                else ""
            ),
        }
    finally:
        safe_close_db(conn)


def _search_kg_facts(
    db: AnyConnection,
    fts_query: str,
    limit: int,
    include_invalid: bool,
    as_of: float | None = None,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
) -> list[dict]:
    """Surfaces structured facts (``subject --[predicate]--> object``) alongside
    the memory results.  Uses the same FTS5 query string as the memories
    search so tokenization is consistent.  Superseded and invalidated facts
    are excluded by default (``include_invalid=False``).

    ``as_of`` is Sprint 5 time-travel: when set, only facts valid at the
    given epoch are returned (via the temporal validity filter from
    ``fact_temporal._temporal_fact_clause``).

    Sprint 1 belief filter params:
    - ``belief_status``: if set, only return facts with this status (active, retracted, deprecated, unconfirmed)
    - ``epistemic_source``: if set, only return facts from this source (agent, auto_save, hook, import, cron)
    - ``fact_type``: if set, only return facts of this type (observation, agent_inference, external_stated, hypothesis, derived)

    A3.3: derived facts (is_entailed=1) are discounted in confidence (×0.8)
    so directly observed facts outrank inferred ones.  is_entailed=0 facts
    are excluded entirely (they were invalidated by a superseded source).

    Returns a list of dicts sorted by FTS5 BM25 rank (best first).  Returns
    an empty list if the kg_facts table is missing or empty — never raises.
    """
    try:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_facts'"
        ).fetchone():
            return []
        # A3.3: is_entailed is added by migration 034; skip the discount logic
        # when the column is absent (e.g. pre-migration test DBs).
        _has_is_entailed = any(
            row[1] == "is_entailed"
            for row in db.execute("PRAGMA table_info(kg_facts)").fetchall()
        )
        invalid_filter = ""
        belief_params: list = []
        if not include_invalid and as_of is None:
            invalid_filter = (
                " AND (kf.invalid_at IS NULL OR kf.invalid_at = '')"
                " AND kf.superseded_by IS NULL"
            )
        elif as_of is not None:
            from fact.fact_temporal import _temporal_fact_clause

            clause, t_params = _temporal_fact_clause(as_of)
            invalid_filter = (
                " AND (kf.superseded_by IS NULL)"
                + clause.replace("f.", "kf.")
            )
            belief_params = t_params
        belief_filter = ""
        if belief_status is not None:
            belief_filter += " AND kf.belief_status = ?"
            belief_params.append(belief_status)
        if epistemic_source is not None:
            belief_filter += " AND kf.epistemic_source = ?"
            belief_params.append(epistemic_source)
        if fact_type is not None:
            belief_filter += " AND kf.fact_type = ?"
            belief_params.append(fact_type)
        _is_entailed_idx = 14  # default offset when is_entailed is NOT selected
        if _has_is_entailed:
            _is_entailed_idx = 15
            select_cols = (
                "kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
                "kf.mention_count, kf.first_seen, kf.last_seen, kf.event_time, "
                "kf.event_time_granularity, kf.contradiction_score, kg_facts_fts.rank, "
                "kf.belief_status, kf.epistemic_source, kf.fact_type, "
                "kf.is_entailed "
            )
        else:
            select_cols = (
                "kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
                "kf.mention_count, kf.first_seen, kf.last_seen, kf.event_time, "
                "kf.event_time_granularity, kf.contradiction_score, kg_facts_fts.rank, "
                "kf.belief_status, kf.epistemic_source, kf.fact_type "
            )
        rows = db.execute(
            "SELECT " + select_cols + "FROM kg_facts_fts "
            "JOIN kg_facts kf ON kf.rowid = kg_facts_fts.rowid "
            f"WHERE kg_facts_fts MATCH ?{invalid_filter}{belief_filter} "
            "ORDER BY kg_facts_fts.rank "
            "LIMIT ?",
            (fts_query, *belief_params, limit),
        ).fetchall()
    except sqlite3.Error:
        logger.warning("KG fact search failed; returning empty list", exc_info=True)
        return []

    results = []
    for r in rows:
        is_entailed = 0
        if _has_is_entailed and len(r) > _is_entailed_idx:
            is_entailed = 1 if r[_is_entailed_idx] else 0
        # A3.3: discount inferred (is_entailed=1) facts by 0.8 so directly
        # observed facts outrank derived knowledge.  Direct facts
        # (is_entailed=0) pass through unchanged.
        confidence = r[4] or 0.0
        if is_entailed == 1:
            confidence = round(confidence * 0.8, 4)
        result_entry: dict = {
            "id": r[0],
            "subject": r[1],
            "predicate": r[2],
            "object": r[3],
            "confidence": confidence,
            "mention_count": r[5],
            "first_seen": r[6],
            "last_seen": r[7],
            "event_time": r[8],
            "event_time_granularity": r[9],
            "contradiction_score": r[10],
            "fts_rank": r[11],
            "belief_status": r[12],
            "epistemic_source": r[13],
            "fact_type": r[14],
        }
        if _has_is_entailed:
            result_entry["is_entailed"] = is_entailed
        results.append(result_entry)
    return results


def _reasoning_expand(db_path: Path, query: str, limit: int = 5) -> list[str]:
    """A3.1: expand a natural-language query using entailment-chain objects.

    When the query contains an entailment-predicate keyword (``is a``,
    ``type of``, ``part of``, ``instance of``, ``subclass of``,
    ``located in``, or ``has part``), look up KG facts that were derived
    via ``entailment_chains`` and return their ``object`` terms as
    expansion tokens.

    Returns ``[]`` (no expansion) when no entailment predicate is detected
    or when the DB lookup yields nothing.
    """
    _ENTAILMENT_PREDICATES = (
        "is_a",
        "is_type_of",
        "subclass_of",
        "instance_of",
        "part_of",
        "has_part",
        "located_in",
    )
    # Normalize query lower-case for predicate detection.
    q_lower = query.lower()
    matched_predicate: str | None = None
    for pred in _ENTAILMENT_PREDICATES:
        # Support both with and without internal underscores in the user query.
        readable = pred.replace("_", " ")
        if readable in q_lower or (pred == "is_a" and re.search(r"\bis\b", q_lower)):
            matched_predicate = pred
            break
    if matched_predicate is None:
        return []
    # Extract entity term: take the longest word sequence around the predicate.
    parts = re.split(
        r"\b(?:is a|is type of|subclass of|instance of|part of|has part|located in|is)\b",
        q_lower,
        flags=re.IGNORECASE,
    )
    entity_candidates = [
        p.strip()
        for p in parts
        if p.strip() and not re.fullmatch(r"[A-Za-z ]{1,4}", p.strip())
    ]
    entity_term = max(entity_candidates, key=len) if entity_candidates else query
    # Collapse multi-word entity into a single LIKE token set.
    tokens = re.findall(r"[A-Za-z][A-Za-z\-_/]+", entity_term)
    like_pattern = "%" + "%".join(t for t in tokens if len(t) > 2) + "%" if tokens else "%" + entity_term + "%"
    try:
        from infra._lazy_imports import connection_pool

        conn = connection_pool.get(str(db_path), timeout=10.0)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT kf.object
                FROM kg_facts kf
                JOIN entailment_chains ec ON ec.derived_fact_id = kf.id
                WHERE kf.predicate IN ('is_a','is_type_of','instance_of',
                                       'part_of','has_part','located_in','subclass_of')
                  AND kf.belief_status = 'active'
                  AND kf.is_entailed = 1
                  AND (kf.subject LIKE ? OR kf.object LIKE ?)
                LIMIT ?
                """,
                (like_pattern, like_pattern, limit),
            ).fetchall()
            return [row[0] for row in rows if row[0]]
        finally:
            try:
                connection_pool.put(conn)
            except Exception as e:
                logger.warning("reasoning_expand: connection_pool.put failed: %s", e)
    except Exception as exc:
        logger.warning("reasoning_expand failed: %s", exc)
        return []


def _fts_search(
    db: AnyConnection,
    fts_query: str,
    limit: int,
    has_fitness: bool,
    repo_filter: str = "",
    tag_filter_sql: str = "",
    tag_filter_params: tuple = (),
    category: str | None = None,
) -> list:
    _base_filter = repo_filter + tag_filter_sql
    if has_fitness:
        params: tuple = (fts_query,)
        if category:
            params = (fts_query, category)
        params = params + tag_filter_params
        return db.execute(
            f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
            "                 m.fitness_score, m.importance, m.pinned, m.last_accessed, m.metadata, m.access_count\n"
            "          FROM memories_fts fts\n"
            "          JOIN tenant_memories m ON m.id = (SELECT id FROM memories WHERE rowid = fts.rowid)\n"
            f"          WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{_base_filter}\n"
            "          ORDER BY fts.rank\n"
            "          LIMIT ?",
            (*params, limit * 2),
        ).fetchall()
    params = (fts_query,)
    if category:
        params = (fts_query, category)
    params = params + tag_filter_params
    return db.execute(
        f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
        "             NULL, NULL, NULL, m.last_accessed, m.metadata, m.access_count\n"
        "      FROM memories_fts fts\n"
        "      JOIN tenant_memories m ON m.id = (SELECT id FROM memories WHERE rowid = fts.rowid)\n"
        f"      WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{_base_filter}\n"
        "      ORDER BY fts.rank\n"
        "      LIMIT ?",
        (*params, limit),
    ).fetchall()


def _fallback_embedding_search(
    db: AnyConnection,
    normalized_query: str,
    db_path: Path,
    limit: int,
    repo_filter: str,
    category: str = "",
    tag_filter_sql: str = "",
    tag_filter_params: tuple = (),
) -> list:
    """Try embedding search as fallback when FTS returns nothing."""
    try:
        from infra._lazy_imports import get_embedding_search

        _es = get_embedding_search()
        _es_results = _es.search(normalized_query, db_path, limit=limit * 2, category=category)
        if not isinstance(_es_results, list) or not _es_results:
            return []

        _es_results = [
            hit
            for hit in _es_results
            if float(hit.get("score", 0.0)) >= _get_embedding_score_threshold()
        ]
        if not _es_results:
            return []
        hit_ids = [hit.get("id") for hit in _es_results if hit.get("id")]
        _base_filter = repo_filter + tag_filter_sql
        _params = ((category,) if category else ()) + tag_filter_params
        rows_map = _fetch_rows_by_ids(db, hit_ids, extra_filter=_base_filter, extra_params=_params)

        fb_rows = []
        for hit in _es_results:
            hit_id = hit.get("id")
            if not hit_id:
                continue
            row = rows_map.get(hit_id)
            if not row:
                continue
            (
                mid,
                content,
                source_file,
                tags_json,
                created,
                fitness,
                importance,
                pinned,
            ) = row[:8]
            last_accessed = row[8] if len(row) > 8 else None
            metadata_json = row[9] if len(row) > 9 else None
            score = float(hit.get("score", 0.0))
            rank = -score
            fb_rows.append(
                (
                    mid,
                    content,
                    source_file,
                    tags_json,
                    created,
                    rank,
                    fitness,
                    importance,
                    pinned,
                    last_accessed,
                    metadata_json,
                )
            )
        return fb_rows
    except Exception as e:
        logger.warning("_fallback_embedding_search failed: %s", e)
        return []


def _hybrid_fusion(
    db: AnyConnection,
    results: list,
    normalized_query: str,
    fts_query: str,
    db_path: Path,
    limit: int,
    repo_filter: str,
    category: str | None = None,
) -> list:
    """Merge FTS, semantic, chunk FTS, and SPLADE results using RRF."""
    try:
        from infra._lazy_imports import get_config, get_embedding_search

        _es = get_embedding_search()
        _overfetch = int(getattr(get_config(), "hybrid_semantic_overfetch", 3))
        _rrf_k = int(getattr(get_config(), "hybrid_rrf_k", 60))
        _rank_scale = float(getattr(get_config(), "hybrid_rank_proxy_scale", 30.0))
        _fts_w = float(getattr(get_config(), "hybrid_fts_weight", 1.0))
        _sem_w = float(getattr(get_config(), "hybrid_semantic_weight", 1.0))
        _chunk_fts_w = float(getattr(get_config(), "hybrid_chunk_fts_weight", 0.8))
        _splade_w = float(getattr(get_config(), "hybrid_splade_weight", 0.6))
        _es_results = _es.search(normalized_query, db_path, limit=limit * _overfetch)
        if not isinstance(_es_results, list):
            _es_results = []
        fts_ranked = [r[0] for r in results]
        sem_ranked = [h.get("id") for h in _es_results if h.get("id")]

        # Get chunk-level FTS hits and build their ranked parent ID list
        chunk_hits = _search_chunks_enhanced(db, fts_query, limit=limit * 2)
        merged_chunks = _merge_chunk_hits(chunk_hits)
        chunk_fts_ranked = [p_id for p_id, _, _, _, _ in merged_chunks]

        # SPLADE sparse search (Phase 4)
        splade_ranked = []
        try:
            from infra.splade_encoder import encode_sparse
            from search.splade_index import splade_search
            query_sparse = encode_sparse(normalized_query)
            if query_sparse:
                splade_results = splade_search(db, query_sparse, top_k=limit * _overfetch)
                splade_ranked = [mid for mid, _ in splade_results]
        except Exception as _splade_exc:
            logger.debug("SPLADE search skipped: %s", _splade_exc)

        # Fusion over Document FTS, Semantic, Chunk FTS, and SPLADE
        rrf = _reciprocal_rank_fusion(
            [fts_ranked, sem_ranked, chunk_fts_ranked, splade_ranked],
            k=_rrf_k,
            weights=[_fts_w, _sem_w, _chunk_fts_w, _splade_w]
        )
        existing_ids = {r[0]: i for i, r in enumerate(results)}
        new_hit_ids = []
        for hit_id in sem_ranked + chunk_fts_ranked + splade_ranked:
            if hit_id and hit_id not in existing_ids and hit_id not in new_hit_ids:
                new_hit_ids.append(hit_id)
        new_hit_rows = _fetch_rows_by_ids(db, new_hit_ids, extra_filter=repo_filter, extra_params=(category,) if category else ())
        semantic_only = []
        for hit_id in new_hit_ids:
            row = new_hit_rows.get(hit_id)
            if not row:
                continue
            (
                mid,
                content,
                source_file,
                tags_json,
                created,
                fitness,
                importance,
                pinned,
            ) = row[:8]
            last_accessed = row[8] if len(row) > 8 else None
            metadata_json = row[9] if len(row) > 9 else None
            rank_proxy = -rrf.get(hit_id, 0.0) * _rank_scale
            semantic_only.append(
                (
                    mid,
                    content,
                    source_file,
                    tags_json,
                    created,
                    rank_proxy,
                    fitness,
                    importance,
                    pinned,
                    last_accessed,
                    metadata_json,
                )
            )
        # P1-8 fix: merge FTS + semantic results and sort by RRF score.
        # Update FTS results' rank (index 5) with RRF score, then merge and sort.
        merged = []
        for r in results:
            hit_id = r[0]
            rrf_score = rrf.get(hit_id, 0.0)
            # Replace index 5 (FTS rank) with RRF-based rank_proxy
            merged.append(r[:5] + (-rrf_score * _rank_scale,) + r[6:])
        merged.extend(semantic_only)
        merged.sort(key=lambda x: x[5])  # sort by rank_proxy (index 5)
        return merged
    except Exception as e:
        _phase_inc("search.hybrid_fusion", e)
        logger.warning("hybrid_fusion failed: %s", e)
        return results


def _enhance_with_chunks(
    db: AnyConnection,
    results: list,
    fts_query: str,
    limit: int,
    include_invalid: bool,
    repo_filter: str,
    category: str | None = None,
) -> list:
    """Add chunk-level matches to results."""
    try:
        chunk_hits = _search_chunks_enhanced(db, fts_query, limit=limit * 2)
        if not chunk_hits:
            return results
        merged = _merge_chunk_hits(chunk_hits)
        seen_ids = {r[0] for r in results}
        chunk_parent_ids = [p_id for p_id, _, _, _, _ in merged if p_id not in seen_ids]
        chunk_rows = _fetch_rows_by_ids(db, chunk_parent_ids, extra_filter=repo_filter, extra_params=(category,) if category else ())
        # P0-6 fix (2026-06-23): batch the valid_to check instead of
        # one query per chunk hit. Previously we ran a separate
        # ``SELECT valid_to FROM memories WHERE id = ?`` inside the
        # loop below, which is an N+1 query that can dominate search
        # latency for queries that return many chunk hits. Now we
        # collect all the unique parent_ids that need checking, do a
        # single ``SELECT id, valid_to FROM memories WHERE id IN
        # (...)``, and build a set of invalid ids to skip.
        invalid_ids: set[str] = set()
        if not include_invalid:
            cols = _get_memories_columns(db)
            if "valid_to" in cols:
                check_ids = [
                    p_id
                    for p_id, _, _, _, _ in merged
                    if p_id not in seen_ids and p_id in chunk_rows
                ]
                if check_ids:
                    placeholders = ",".join("?" * len(check_ids))
                    _rows = db.execute(
                        f"SELECT id, valid_to FROM tenant_memories WHERE id IN ({placeholders})",
                        check_ids,
                    ).fetchall()
                    invalid_ids = {row[0] for row in _rows if row[1] not in (None, "")}
        for parent_id, chunk_idx, chunk_text, chunk_rank, chunk_count in merged:
            if parent_id in seen_ids:
                continue
            if parent_id in invalid_ids:
                continue
            row = chunk_rows.get(parent_id)
            if not row:
                continue
            (
                mid,
                content,
                source_file,
                tags_json,
                created,
                fitness,
                importance,
                pinned,
            ) = row[:8]
            last_accessed = row[8] if len(row) > 8 else None
            metadata_json = row[9] if len(row) > 9 else None
            access_count = row[10] if len(row) > 10 else 1
            boost = 1.0 + (chunk_count - 1) * 0.1 if chunk_count > 1 else 1.0
            results.append(
                (
                    mid,
                    content,
                    source_file,
                    tags_json,
                    created,
                    chunk_rank * boost,
                    fitness,
                    importance,
                    pinned,
                    last_accessed,
                    metadata_json,
                    access_count,
                )
            )
            seen_ids.add(parent_id)
    except Exception as _chunk_exc:
        _phase_inc("search.chunk_enhancement", _chunk_exc)
        logger.warning("chunk_enhancement failed: %s", _chunk_exc)
    return results


def _format_search_results(
    results_to_display: list,
    query: str,
    rerank: bool,
    result_items: list,
    backlinks_map: dict,
) -> list[str]:
    """Build human-readable output lines from ranked results.

    T10 note: the "Related facts" section is no longer built here — it
    is appended in ``_build_search_result_envelope`` AFTER all post-Phase-10
    regeneration passes, so it survives the safety demoting, quality gates,
    user profiling, strong-match boost, and save-hint floater passes which
    each call ``_format_search_results`` without T10 context.
    """
    output = [
        f"Search results for: '{query}' (Re-ranked)"
        if rerank
        else f"Search results for: '{query}'"
    ]
    for i, r in enumerate(results_to_display, 1):
        (
            note_id,
            content,
            source_file,
            tags_json,
            created,
            rank,
            final_score,
            fitness_score,
            importance_val,
            pinned,
        ) = r[:10]
        metadata_json = r[11] if len(r) > 11 else None
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        tags_str = ", ".join(tags) if tags else "none"
        backlinks = backlinks_map.get(note_id, [])
        backlinks_str = (
            ", ".join((f"[[{b}]]" for b in backlinks)) if backlinks else "none"
        )
        score_info = (
            f"(Relevance: {final_score:.2f})" if rerank else f"(Rank: {-rank:.2f})"
        )
        if fitness_score is not None:
            score_info += f" | Fitness: {fitness_score:.2f} | Importance: {importance_val} | Pinned: {('yes' if pinned else 'no')}"
        summary_line = ""
        if metadata_json:
            try:
                meta = (
                    json.loads(metadata_json)
                    if isinstance(metadata_json, str)
                    else metadata_json
                )
                if meta and meta.get("auto_summary"):
                    summary_line = f"\n    Summary: {meta['auto_summary'][:300]}"
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        output.append(
            f"[{i}] {note_id} {score_info}\n    Source: memory/{source_file}\n    Tags: {tags_str}\n    Backlinks: {backlinks_str}\n    Created: {created}{summary_line}\n    Content:\n    {content.strip()}"
        )
    return output


def _apply_safety_demoting(
    result_items: list, output: list[str], results_to_display: list
) -> tuple[list, list[str], list]:
    """Apply injection-detection safety demoting to results."""
    try:
        import memory_injection

        _rtd_by_id = {r[0]: r for r in results_to_display}
        _demote_input = [
            {
                "id": item["id"],
                "content": _rtd_by_id.get(item["id"], (None, ""))[1] or "",
                "score": float(item.get("final_score") or 0.0),
            }
            for item in result_items
        ]
        _demoted = memory_injection.demote_results_by_injection(_demote_input)
        _id_to_idx = {item["id"]: i for i, item in enumerate(result_items)}
        _new_order = []
        for _d in _demoted:
            if _d["id"] in _id_to_idx:
                _new_order.append(_id_to_idx[_d["id"]])
        _seen = set(_new_order)
        for _i in range(len(result_items)):
            if _i not in _seen:
                _new_order.append(_i)
        result_items = [result_items[_i] for _i in _new_order]
        _new_output = [output[0]]
        for _old_idx in _new_order:
            _new_output.append(output[_old_idx + 1])
        output = _new_output
        results_to_display = [results_to_display[_i] for _i in _new_order]
    except Exception as e:
        _phase_inc("search.safety_demoting", e)
        logger.warning("safety_demoting failed: %s", e)
    return result_items, output, results_to_display


def _rerank_results(
    *,
    results: list,
    query: str,
    db_path: Path,
    has_fitness: bool,
    rerank: bool,
    boost_pinned: bool,
    recency_weight: float,
    limit: int,
    deep_rerank: bool,
    as_of: float | None = None,
) -> tuple[list, Optional[dict]]:
    """Phase 9 of search_memories: compute final scores and rerank.

    Returns ``(results_to_display, ctr_weights)``:

    * ``results_to_display`` is the per-row tuple list (note_id,
      content, source_file, tags_json, created, rank, final_score,
      fitness_score, importance_val, pinned, last_accessed,
      metadata_json) — ready for the build-output phase.
    * ``ctr_weights`` is the per-query channel-weight dict used to
      compute final scores, returned for CTR feedback persistence.
      ``None`` when ``rerank=False`` or no fitness column.

    When the table has no ``fitness_score`` column or ``rerank`` is
    disabled, returns the input rows reshaped into the 12-tuple form
    with ``-rank`` as the final_score — a sensible default that keeps
    the result list sorted by FTS rank even without reranking.
    """
    if not (has_fitness and rerank):
        # No reranking: pass through with -rank as final_score.
        out = []
        for r in results:
            last_accessed_col = r[9] if len(r) > 9 else None
            metadata_json = r[10] if len(r) > 10 else None
            access_count = r[11] if len(r) > 11 else 1
            out.append(
                (
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    -r[5],
                    None,
                    None,
                    None,
                    last_accessed_col,
                    metadata_json,
                    access_count,
                    None,
                )
            )
        # RANK-FIRST LOCK (PR1.1): the no-rerank pass-through must not
        # mutate the ranking score. Order is fixed by -rank (set above);
        # enrichment is attached as order-invariant envelope fields by
        # _apply_post_rank_metadata in Phase 10.
        return out[:limit], None

    _qtype = _detect_query_type(query)
    # Phase 6: per-query-type CTR-learned weights override global prior
    from search.scoring import apply_query_type_weights
    _qweights = apply_query_type_weights(_qtype)
    # Legacy global CTR tuning (gated behind MEMORY_CTR_TUNING=1)
    _ctr_w = compute_channel_weights(db_path)
    if _ctr_w is not None:
        _qweights = _ctr_w
    scored = []
    # BM25 normalization: rescale raw FTS5 ranks to [0, 1] before sigmoid
    # so BM25 contributes meaningful discrimination regardless of IDF magnitude.
    _rank_normalized = _normalize_bm25_ranks(results)
    for r in _rank_normalized:
        (
            note_id,
            content,
            source_file,
            tags_json,
            created,
            rank,
            fitness,
            importance,
            pinned,
        ) = r[:9]
        last_accessed = r[9] if len(r) > 9 else None
        metadata_json = r[10] if len(r) > 10 else None
        access_count = r[11] if len(r) > 11 else 1
        final_score = _compute_final_score(
            ScoreContext(
                rank=rank,
                fitness=fitness,
                importance=importance,
                pinned=pinned,
                created=created,
                tags_json=tags_json,
                query=query,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                weights=_qweights,
                now_ts=as_of,
            )
        )
        importance_val = importance if importance is not None else 3
        fitness_score = fitness if fitness is not None else 0.5
        scored.append(
            (
                note_id,
                content,
                source_file,
                tags_json,
                created,
                rank,
                final_score,
                fitness_score,
                importance_val,
                pinned,
                last_accessed,
                metadata_json,
                access_count,
                None,
            )
        )

    scored = _strong_match_float(scored)
    # PR1.2: Single Monotonic CE. Exactly ONE CE stage rewrites r[6],
    # selected by query type (weak default / chunk for long-multi-part /
    # conversational / deep gated on MEMORY_CE_DEEP). This removes the
    # PR1.1 dual-CE ambiguity where weak+chunk both rewrote r[6] and the
    # last writer owned the order. Late-interaction (a separate reranker
    # family, NOT a CE stage) still runs below, and the PR1.1 final sort
    # re-asserts the ranking order afterwards. No CE stage runs after this.
    # PR1.2 (option 2): ONE deterministic CE stage. The non-deep path
    # ("combined") reproduces the validated PR1.1 baseline exactly: a weak
    # hand-rolled 0.6 pre-pass over the top limit*2, then a chunk ms-marco
    # 0.7 pass over the top limit*3 (with the baseline p80 pre-filter),
    # combined into ONE r[6] write per item. The deep path ("deep") runs the
    # combined baseline then an optional Qwen3-Reranker top-30 refinement
    # that degrades gracefully to combined when the model is unavailable.
    _ce_mode = _select_ce_mode(query, deep_rerank)
    _ce_weak_k = min(len(scored), limit * 2)
    _ce_chunk_k = min(len(scored), limit * 3)
    out = _apply_single_ce_rerank(
        query, scored, top_k=_ce_chunk_k, mode=_ce_mode,
        weak_k=_ce_weak_k, chunk_k=_ce_chunk_k,
    )
    out = _apply_late_interaction_rerank(query, out, top_k=min(len(out), limit * 2))
    # ColBERT MaxSim reranking (Phase 3): late-interaction via per-token
    # embeddings.  Only fires when index is populated, candidates ≤ 30,
    # and query has ≥ 3 tokens.  Falls through unchanged otherwise.
    try:
        from search.colbert_rerank import colbert_rerank
        from infra.db import open_db
        _colbert_conn = open_db(db_path)
        try:
            out = colbert_rerank(_colbert_conn, query, out, db_path=db_path)
        finally:
            try:
                _colbert_conn.close()
            except Exception:
                pass
    except Exception as _cb_exc:
        logger.debug("colbert_rerank skipped: %s", _cb_exc)
    # Answer-level reranking (Phase 5): score best snippet per candidate.
    # Uses cross-encoder on extracted snippets, with pre-computed cache.
    try:
        from search.answer_rerank import answer_rerank
        from infra.db import open_db
        _answer_conn = open_db(db_path)
        try:
            out = answer_rerank(_answer_conn, query, out, db_path=db_path)
        finally:
            try:
                _answer_conn.close()
            except Exception:
                pass
    except Exception as _ar_exc:
        logger.debug("answer_rerank skipped: %s", _ar_exc)
    # RANK-FIRST LOCK (PR1.1): order is owned exclusively by the CE /
    # late-interaction rerankers above, which sort on r[6] (the CE-blended
    # final_score). The four historical enrichment passes (temporal decay,
    # Jaccard surprise, concept boost, centrality boost) must NOT mutate
    # r[6] or re-sort here. They are attached as order-invariant envelope
    # fields by _apply_post_rank_metadata in Phase 10. Re-assert the
    # ranking order so any future in-place score mutation cannot leak into
    # result ordering.
    out = sorted(
        out,
        key=lambda r: (float(r[6]) if r[6] is not None else 0.0),
        reverse=True,
    )
    return out[:limit], _qweights


def _build_result_items(
    *, db: AnyConnection, results_to_display: list, query: str, rerank: bool,
    db_path: Any = None,
    as_of: Optional[float] = None,
) -> tuple[list, list[str], dict]:
    """Phase 10 of search_memories: build the public result list and output.

    Two responsibilities:

    1. Build ``result_items`` (a list of dicts) for the public API
       and gather backlinks (forward + reverse) for each result row.
    2. Call ``_format_search_results`` to produce the human-readable
       ``output`` string.

    T10: when ``related_facts`` is provided, appends a "Related facts"
    section to the output and includes the facts in the result envelope
    (caller decides whether to surface it on the wire).

    Backlinks lookups are wrapped in try/except because the
    ``backlinks`` table is optional in some deployments — the
    function must still produce a valid result set if the lookup
    fails.
    """
    result_items = []
    result_ids = [r[0] for r in results_to_display]
    backlinks_map: dict = {}
    category_map: dict = {}
    if result_ids:
        ph = ",".join("?" * len(result_ids))
        try:
            for row in db.execute(
                f"SELECT target_id, source_id FROM backlinks WHERE target_id IN ({ph})",
                result_ids,
            ).fetchall():
                backlinks_map.setdefault(row[0], []).append(row[1])
            for row in db.execute(
                f"SELECT source_id, target_id FROM backlinks WHERE source_id IN ({ph})",
                result_ids,
            ).fetchall():
                backlinks_map.setdefault(row[0], []).append(row[1])
        except Exception as _oe:
            logger.warning("backlinks fetch failed: %s", _oe)
        try:
            for row in db.execute(
                f"SELECT id, category FROM tenant_memories WHERE id IN ({ph})",
                result_ids,
            ).fetchall():
                category_map[row[0]] = row[1]
        except Exception as _ce:
            logger.warning("category fetch failed: %s", _ce)
    for r in results_to_display:
        (
            note_id,
            content,
            source_file,
            tags_json,
            created,
            rank,
            final_score,
            fitness_score,
            importance_val,
            pinned,
        ) = r[:10]
        last_accessed = r[10] if len(r) > 10 else None
        metadata_json = r[11] if len(r) > 11 else None
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        backlinks = backlinks_map.get(note_id, [])
        auto_summary = None
        meta = None
        if metadata_json:
            try:
                meta = (
                    json.loads(metadata_json)
                    if isinstance(metadata_json, str)
                    else metadata_json
                )
                if meta and meta.get("auto_summary"):
                    auto_summary = meta["auto_summary"]
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        result_items.append(
            {
                "id": note_id,
                "content": content,
                "source_file": source_file,
                "tags": tags,
                "created": created,
                "rank": rank,
                "final_score": final_score,
                "fitness_score": fitness_score,
                "importance": importance_val,
                "pinned": pinned,
                "backlinks": backlinks,
                "last_accessed": last_accessed,
                "metadata": meta if meta is not None else {},
                "summary": auto_summary,
                "category": category_map.get(note_id),
            }
        )
    output = _format_search_results(
        results_to_display,
        query,
        rerank,
        result_items,
        backlinks_map,
    )
    # RANK-FIRST LOCK (PR1.1): attach enrichment as order-invariant
    # envelope fields. This is the ONLY post-CE enrichment site; it never
    # re-sorts or mutates the ranking final_score.
    if db_path is not None:
        result_items = _apply_post_rank_metadata(
            result_items, query, db_path, as_of=as_of
        )
    return result_items, output, backlinks_map


def _apply_strong_match_boost(
    *,
    result_items: list,
    output: list[str],
    results_to_display: list,
    query: str,
    rerank: bool,
    backlinks_map: dict,
) -> tuple[list, list[str], list]:
    """QB6 final pass: hoist a high-confidence match to position 0.

    If any row's FTS5 rank converts (via the standard ``1/(1+exp(r))``
    sigmoid) to a confidence ≥ 0.95, that row is moved to the top of
    both ``result_items`` and ``results_to_display``, and the
    human-readable ``output`` is regenerated to reflect the new
    order.

    No-op if no row crosses the 0.95 threshold or if the strong row
    is already at index 0.  All exceptions are swallowed because this
    pass is a UX optimization — a failure here must never break a
    successful search.
    """
    try:
        strong_id = None
        for r in results_to_display or []:
            try:
                rv = float(r[5]) if len(r) > 5 else 0.0
            except (TypeError, ValueError):
                rv = 0.0
            rv = max(-60.0, min(60.0, rv))
            bm = 1.0 / (1.0 + math.exp(rv))
            if bm >= 0.95:
                strong_id = r[0]
                break
        if strong_id is None or not result_items:
            return result_items, output, results_to_display
        hit_idx = next(
            (i for i, ri in enumerate(result_items) if ri.get("id") == strong_id),
            None,
        )
        if hit_idx is None or hit_idx == 0:
            return result_items, output, results_to_display
        strong = result_items.pop(hit_idx)
        result_items.insert(0, strong)
        disp_idx = next(
            (i for i, rr in enumerate(results_to_display) if rr[0] == strong_id),
            None,
        )
        if disp_idx is not None:
            disp_row = results_to_display.pop(disp_idx)
            results_to_display.insert(0, disp_row)
        try:
            output = _format_search_results(
                results_to_display,
                query,
                rerank,
                result_items,
                backlinks_map,
            )
        except Exception as _oe:
            logger.warning("_format_search_results failed: %s", _oe)
        return result_items, output, results_to_display
    except Exception as e:
        _phase_inc("search.strong_match_boost", e)
        logger.warning("_apply_strong_match_boost failed: %s", e)
        return result_items, output, results_to_display


def _cache_store_result(cache_key: str, result: dict) -> None:
    """Store a search result in the LRU cache and enforce the size cap.

    The 3-line "set + move_to_end + pop oldest" sequence appears in
    every code path that returns a result dict from search_memories.
    Centralizing it here keeps the cache-eviction policy in one place
    — if SEARCH_CACHE_MAX is ever changed (e.g. per-deployment tuning)
    this is the only spot to touch.
    """
    from infra.cache import cache_put, register_cache_note_ids

    note_ids = [
        item.get("id", "")
        for item in (result.get("results") or result.get("result_items") or [])
        if item.get("id")
    ]
    cache_put(cache_key, result, max_size=SEARCH_CACHE_MAX)
    if note_ids:
        try:
            register_cache_note_ids(cache_key, note_ids)
        except Exception as e:
            logger.warning("register_cache_note_ids failed: %s", e)


def _build_empty_result_with_hint(
    *, cache_key, query, db_path, hint, related_facts=None
) -> dict:
    """Build the standard "no results" envelope used by the early-return paths.

    Used when FTS5 + embedding fallback return nothing, and when the
    temporal filter eliminates everything.  Caches and returns the
    result via ``_cache_store_result``.

    T10: when ``related_facts`` is non-empty, append the standard
    "Related facts (KG)" section to the output and attach the facts
    to the envelope.  This is how the empty-DB path can still surface
    matching KG facts (the common case when the user has facts but
    no memories yet).
    """
    if hint:
        output = f"No memories matched the query: '{query}'. {hint}"
    else:
        output = f"No memories matched the query: '{query}'"
    facts_section: list[str] = []
    if related_facts:
        facts_section.append("")
        facts_section.append("--- Related facts (KG) ---")
        for j, fact in enumerate(related_facts, 1):
            et = fact.get("event_time")
            et_str = ""
            if et is not None:
                from datetime import datetime as _dt, timezone as _tz

                try:
                    et_str = f" [event_time={_dt.fromtimestamp(et, tz=_tz.utc).strftime('%Y-%m-%d')}]"
                except (ValueError, OSError):
                    pass
            conf = fact.get("confidence", 0.0) or 0.0
            facts_section.append(
                f"  [F{j}] {fact['subject']} --[{fact['predicate']}]--> "
                f"{fact['object']} (conf={conf:.2f}, mentions={fact.get('mention_count', 1)})"
                f"{et_str}"
            )
        output = output + "\n" + "\n".join(facts_section)
    result = {
        "results": [],
        "count": 0,
        "output": output,
        "suggestions": _build_zero_result_suggestions(db_path, query),
        "agent_scope": _get_agent_scope(),
        "query_id": uuid.uuid4().hex,
    }
    if related_facts:
        result["related_facts"] = related_facts
    _cache_store_result(cache_key, result)
    return result


def _record_last_accessed(db: AnyConnection | None, result_items: list) -> None:
    """Phase 12 of search_memories: stamp last_accessed on every result row.

    Bumps ``last_accessed`` to the current ISO timestamp in a single
    batched UPDATE for every result note.  No-op if the result set is
    empty.  All exceptions are swallowed — adaptive retention depends
    on this column but a failure to record is non-fatal.
    """
    if not result_items:
        return
    if db is None:
        return
    try:
        import datetime as _dt

        now_iso = _dt.datetime.now().isoformat(timespec="seconds")
        ids = [r["id"] for r in result_items]
        _CHUNK_SIZE = 998  # 1 slot reserved for the timestamp param
        for i in range(0, len(ids), _CHUNK_SIZE):
            chunk = ids[i : i + _CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            db.execute(
                f"UPDATE memories SET last_accessed = ? WHERE id IN ({placeholders})",
                [now_iso] + chunk,
            )
        db.commit()
    except Exception as e:
        _phase_inc("search.record_last_accessed", e)
        logger.warning("_record_last_accessed failed: %s", e)


def _build_search_result_envelope(
    *,
    result_items: list,
    output: list[str],
    results_to_display: list,
    synthesize: bool,
    query: str,
    max_synthesis_sentences: int,
    related_facts: Optional[list[dict]] = None,
) -> dict:
    """Build the final public-API result dict for a search call.

    Joins the per-row ``output`` lines into a single string, attaches
    a fresh ``query_id`` (used downstream for CTR feedback correlation),
    and optionally calls the synthesis pass when ``synthesize=True``.

    T10: if ``related_facts`` is provided:
      - attaches them under the ``related_facts`` key on the envelope
      - appends a "Related facts (KG)" section to the joined output
        (this is done HERE rather than in _format_search_results so
        it survives the post-Phase-10 regeneration passes — safety
        demoting, quality gates, user profiling, strong-match boost,
        and save-hint floater all rewrite the output list).

    The synthesis step is best-effort: a failure to synthesize must
    never block the user from getting the underlying results.

    Args:
        result_items: List of public-API result dicts (one per row).
        output: List of human-readable output lines (one per result).
        results_to_display: Raw DB row tuples for result formatting.
        synthesize: If True, call ``_bb1_synthesize`` and attach the
            ``synthesis`` key to the envelope.
        query: The original search query string (used for synthesis).
        max_synthesis_sentences: Maximum sentences in synthesis output.
        related_facts: Optional list of KG fact dicts to attach.

    Returns:
        A public-API result dict with ``results``, ``count``,
        ``output``, ``raw_results``, ``query_id``, ``agent_scope``,
        and optionally ``synthesis`` and ``related_facts``.
    """
    # T10: build the related-facts section BEFORE joining so the
    # regeneration passes can't strip it.
    facts_section: list[str] = []
    if related_facts:
        facts_section.append("")
        facts_section.append("--- Related facts (KG) ---")
        for j, fact in enumerate(related_facts, 1):
            et = fact.get("event_time")
            et_str = ""
            if et is not None:
                from datetime import datetime as _dt, timezone as _tz

                try:
                    et_str = f" [event_time={_dt.fromtimestamp(et, tz=_tz.utc).strftime('%Y-%m-%d')}]"
                except (ValueError, OSError):
                    pass
            conf = fact.get("confidence", 0.0) or 0.0
            facts_section.append(
                f"  [F{j}] {fact['subject']} --[{fact['predicate']}]--> "
                f"{fact['object']} (conf={conf:.2f}, mentions={fact.get('mention_count', 1)})"
                f"{et_str}"
            )
    result_str = "\n\n".join(output + facts_section)
    query_id = uuid.uuid4().hex
    result = {
        "results": result_items,
        "count": len(result_items),
        "output": result_str,
        "raw_results": results_to_display,
        "query_id": query_id,
    }
    if related_facts:
        result["related_facts"] = related_facts
    if synthesize and result_items:
        try:
            synth = _bb1_synthesize(
                query, results_to_display, max_sentences=max_synthesis_sentences
            )
            result["synthesis"] = synth
        except Exception as e:
            logger.warning("synthesis failed: %s", e)
    try:
        from agent_context import get_agent
        result["agent_scope"] = get_agent().namespace
    except (ImportError, AttributeError):
        result["agent_scope"] = "default"
    return result


def _apply_quality_gates(
    *,
    result_items: list,
    output: list[str],
    results_to_display: list,
    query: str,
    rerank: bool,
    backlinks_map: dict,
) -> tuple[list, list[str], list]:
    """Phase 11b: drop low-quality results via the quality_gates filter.

    Only acts when ``quality_gates.QUALITY_GATES_ENABLED`` is true
    and the result set is non-empty.  When the filter removes any
    rows, the human-readable output is regenerated to match.

    All exceptions are swallowed — quality gates are advisory; a
    failure here must never block a successful search.
    """
    try:
        import quality_gates as qg

        if getattr(qg, "QUALITY_GATES_ENABLED", False) and result_items:
            result_items, qg_stats = qg.filter_results(result_items)
            if qg_stats.get("filtered", 0) > 0:
                kept_ids = {ri["id"] for ri in result_items}
                results_to_display[:] = [
                    r for r in results_to_display if r[0] in kept_ids
                ]
                output = _format_search_results(
                    results_to_display,
                    query,
                    rerank,
                    result_items,
                    backlinks_map,
                )
    except Exception as e:
        _phase_inc("search.quality_gates", e)
        logger.warning("quality_gates failed: %s", e)
    return result_items, output, results_to_display


def _apply_user_profiling(
    *,
    result_items: list,
    output: list[str],
    results_to_display: list,
    query: str,
    rerank: bool,
    backlinks_map: dict,
    db_path: Path,
) -> tuple[list, list[str], list]:
    """Phase 11c: rerank result list per the user's stored profile.

    Only acts when ``user_profile.PROFILE_ENABLED`` is true and the
    result set is non-empty.  Pulls the active profile from the DB
    and reorders results to match.

    All exceptions are swallowed — personalization is a UX
    optimization; a failure here must never block a successful search.
    """
    try:
        import user_profile as up

        if getattr(up, "PROFILE_ENABLED", False) and result_items:
            profile = up.get_user_profile(db_path=str(db_path))
            result_items = up.personalize_results(result_items, profile=profile)
            kept_ids = {ri["id"] for ri in result_items}
            results_to_display[:] = [
                r for r in results_to_display if r[0] in kept_ids
            ]
            output = _format_search_results(
                results_to_display,
                query,
                rerank,
                result_items,
                backlinks_map,
            )
    except Exception as e:
        _phase_inc("search.user_profiling", e)
        logger.warning("user_profiling failed: %s", e)
    return result_items, output, results_to_display


def _record_search_telemetry(
    *, db: AnyConnection | None, query_id: str, result_items: list, ctr_weights: Optional[dict]
) -> None:
    """Record CTR feedback and adaptive-retention access events for the result set.

    Two side-effects, both best-effort:

    1. Write a row to ``memory_ctr_feedback`` so the next CTR computation
       can correlate this query's result set with user-click behavior.
    2. Call ``adaptive_retention.record_access`` for each result row so
       the per-note fitness decay respects the fresh access.

    All exceptions are swallowed — telemetry is informational, not a
    precondition for the user seeing results.
    """
    if db is None:
        return
    try:
        db.execute(
            "INSERT OR REPLACE INTO memory_ctr_feedback "
            "(id, query_id, returned_at, source, ranking_params) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "__search__",
                query_id,
                time.time(),
                "search",
                json.dumps({"weights": ctr_weights}) if ctr_weights else "{}",
            ),
        )
        db.commit()
    except Exception as e:
        _phase_inc("search.telemetry.ctr_feedback", e)
        logger.warning("record_search_telemetry CTR failed: %s", e)
    try:
        from adaptive_retention import record_access

        for r in result_items:
            record_access(db, r.get("id", ""), source="search")
        db.commit()
    except Exception as e:
        _phase_inc("search.telemetry.adaptive_retention", e)
        logger.warning("record_search_telemetry adaptive_retention failed: %s", e)


def _record_search_phase_latencies(*, db, query_id: str, phase_latencies: dict[str, float]) -> None:
    """Persist per-phase latency to the search_phase_stats table.

    Best-effort: never propagates. Writes one row per phase with the
    latency in milliseconds and a UTC ISO timestamp for aggregation.
    """
    try:
        if not phase_latencies:
            return
        now_ts = time.time()
        rows = [
            (query_id, name, latency_ms, now_ts)
            for name, latency_ms in phase_latencies.items()
        ]
        db.executemany(
            "INSERT INTO search_phase_stats (query_id, phase_name, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        db.commit()
    except Exception as e:
        logger.warning("_record_search_phase_latencies failed: %s", e)


def _apply_save_hint_floater(
    *,
    db: AnyConnection,
    db_path: Path,
    result_items: list,
    output: list[str],
    results_to_display: list,
    query: str,
    rerank: bool,
    backlinks_map: dict,
) -> tuple[list, list[str], list]:
    """Defense-in-depth: surface a very-recent save even if FTS hasn't seen it.

    Reads the ``recent_save_hint`` table (set by the post-save hook
    on a prior save) and, if the hinted note is in the FTS5 index but
    not yet in the result set, prepends it to the result list.

    The hint covers the small window between a save committing and
    the FTS5 trigger / search-cache catching up.  Without this pass,
    a save-then-immediate-search call could return a result set
    missing the just-saved note.  All exceptions are swallowed —
    a missing or stale hint is non-fatal.
    """
    try:
        from recent_save_hint import recent_save_for

        hint = recent_save_for(str(db_path))
    except (ImportError, AttributeError) as _e:
        _phase_inc("search.save_hint_floater", _e)
        return result_items, output, results_to_display
    if hint is None:
        return result_items, output, results_to_display
    hint_id, _hint_ts = hint
    try:
        hint_in_fts = (
            db.execute(
                "SELECT 1 FROM memories_fts fts "
                "JOIN memories m ON m.rowid = fts.rowid "
                "WHERE memories_fts MATCH ? AND m.id = ? AND m.deleted_at IS NULL",
                (hint_id, hint_id),
            ).fetchone()
            is not None
        )
    except sqlite3.Error as _fts_e:
        _phase_inc("search.save_hint_floater", _fts_e)
        return result_items, output, results_to_display
    if not (hint_in_fts and result_items):
        return result_items, output, results_to_display
    if any(ri.get("id") == hint_id for ri in result_items):
        return result_items, output, results_to_display
    try:
        floater_row = db.execute(
            "SELECT id, content, source_file, tags, created_at, "
            "fitness_score, importance, pinned, last_accessed, "
            "metadata, valid_to, 0 AS rank, 0.5 AS final_score "
            "FROM tenant_memories WHERE id = ? AND deleted_at IS NULL",
            (hint_id,),
        ).fetchone()
    except sqlite3.Error as _fl_e:
        _phase_inc("search.save_hint_floater", _fl_e)
        return result_items, output, results_to_display
    if floater_row is None:
        return result_items, output, results_to_display
    floater_item = {
        "id": floater_row[0],
        "source_file": floater_row[2],
        "tags": json.loads(floater_row[3]) if floater_row[3] else [],
        "created": floater_row[4],
        "rank": 0,
        "final_score": 1.0,
        "fitness_score": floater_row[5] or 0.5,
        "importance": floater_row[6] or 3,
        "pinned": floater_row[7] or 0,
        "backlinks": [],
        "summary": None,
    }
    result_items.insert(0, floater_item)
    # Build floater tuple in the canonical results_to_display column
    # order: (id, content, source_file, tags, created, rank,
    #  final_score, fitness, importance, pinned, last_accessed,
    #  metadata, access_count, avg_dist)
    floater_tuple = (
        floater_row[0],
        floater_row[1],
        floater_row[2],
        floater_row[3],
        floater_row[4],
        0.0,
        1.0,
        floater_row[5],
        floater_row[6],
        floater_row[7],
        floater_row[8],
        floater_row[9],
        1,
        None,
    )
    results_to_display.insert(0, floater_tuple)
    try:
        output = _format_search_results(
            results_to_display,
            query, rerank, result_items, backlinks_map,
        )
    except Exception as _oe:
        logger.warning("_format_search_results (fallback) failed: %s", _oe)
    return result_items, output, results_to_display


def _get_agent_scope() -> str:
    try:
        from agent_context import get_agent
        return get_agent().namespace or "default"
    except (ImportError, AttributeError):
        return "default"


def search_memories(
    db_path: Path,
    query: str,
    limit: int = 5,
    include_global: bool = True,
    rerank: bool = True,
    boost_pinned: bool = True,
    recency_weight: float = 0.1,
    include_invalid: bool = True,
    hybrid: bool = True,
    synthesize: bool = False,
    max_synthesis_sentences: int = 5,
    use_history: bool = True,
    safety_wiring: bool = True,
    deep_rerank: bool = False,
    skill_first: bool = False,
    include_facts: bool = True,
    fact_limit: int = 5,
    tenant_id: str = "default",
    light: bool = False,
    as_of: float | None = None,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    memory_source: str | None = None,
    category: str = "",
    tags: list[str] | None = None,
    shared_with_me: bool = False,
) -> dict:
    """Main entry point: 12-phase hybrid search returning ranked memories.

    Orchestrates the full retrieval pipeline: query parsing, FTS5 BM25,
    vector search, ColBERT late-interaction, RRF fusion, cross-encoder
    reranking, temporal decay, neural forget curve, KG concept/centrality
    boost, quality gates, user profiling, and envelope construction.
    Each phase is individually isolated — on failure it increments its
    error counter and the pipeline falls through with degraded results.

    Args:
        db_path: Path to the SQLite memory database.
        query: Natural-language search query.
        limit: Maximum number of results to return.
        include_global: Include memories from all namespaces (not just
            the calling agent's).
        rerank: Enable cross-encoder and late-interaction reranking.
        boost_pinned: Scale pinned notes higher in final ranking.
        recency_weight: Weight for the recency temporal factor.
        include_invalid: Include superseded/invalidated memories.
        hybrid: Enable semantic vector fusion with FTS5.
        synthesize: Generate a synthesis summary alongside results.
        max_synthesis_sentences: Max sentences in synthesis summary.
        use_history: Include session-history context.
        safety_wiring: Run injection-detection safety demoting pass.
        deep_rerank: Use deeper (slower) cross-encoder model.
        skill_first: Return skill matches before memory results.
        include_facts: Include KG facts in the result envelope.
        fact_limit: Max KG facts to return.
        tenant_id: Tenant namespace for multi-tenant isolation.
        light: Skip expensive rerank/personalization passes.
        as_of: Time-travel anchor (epoch seconds) for temporal queries.
        belief_status: Filter KG facts by belief status.
        epistemic_source: Filter KG facts by epistemic source.
        fact_type: Filter KG facts by type.
        memory_source: Filter by memory origin (agent/auto_save/import).
        category: Filter by category slug.
        tags: Filter by tag list (JSON exact-match via LIKE).
        shared_with_me: Append memories shared with the current agent.

    Returns:
        A public-API result dict with keys:
          - results: list of result-item dicts
          - count: int (number of results)
          - output: human-readable result string
          - query_id: UUID for CTR feedback correlation
          - agent_scope: current agent namespace
          - related_facts: (optional) KG facts matching the query
          - phase_errors: (optional) per-phase error counters
          - phase_latencies: (optional) per-phase latency in ms
    """
    if not db_path.exists():
        return {
            "results": [],
            "count": 0,
            "output": _err(
                ErrorCode.DB_ERROR,
                f"Memory database not found in current directory ({db_path}). Run memory_rebuild tool first.",
            ),
            "agent_scope": _get_agent_scope(),
            "query_id": uuid.uuid4().hex,
        }

    # Reset per-call phase latency accumulator so results are not
    # polluted by stale entries from prior invocations.
    with _phase_latencies_lock:
        _phase_latencies.clear()
    _phase_reset()

    # Phase 1: Parse query
    _t0 = time.time()
    normalized_query, fts_query, bare_text, graph_rag_terms = _parse_search_query(
        query, db_path
    )
    _record_phase_latency("parse_query", _t0)
    # A3.2: Reasoning expansion — append entailment-chain objects as OR terms
    # before the cache key is computed so the expanded query is cached.
    _reasoning_t0 = time.time()
    expansion_terms = _reasoning_expand(db_path, query)
    if expansion_terms:
        fts_query = f"{fts_query} OR {' OR '.join(expansion_terms[:5])}"
    _record_phase_latency("reasoning_expand", _reasoning_t0)
    # Drift enforcement for search operations
    try:
        from infra.config_drift import build_drift_report
        from infra.config_drift_policy import enforce, DriftEnforcementError
        _drift_report = build_drift_report()
        enforce(_drift_report, verb="search")
    except DriftEnforcementError:
        raise
    except Exception:
        logger.debug("drift enforcement skipped in search_memories: non-critical error")
    terms = re.findall("[\\w@\\#\\.\\+\\-]+", fts_query, flags=re.UNICODE)
    if not terms:
        return {
            "results": [],
            "count": 0,
            "output": f"No memories matched the query: '{query}'",
            "suggestions": _build_zero_result_suggestions(db_path, query),
            "agent_scope": _get_agent_scope(),
            "query_id": uuid.uuid4().hex,
        }

    # Phase 1b: Skill-first lookup (if requested)
    if skill_first:
        skill_result = _skill_first_lookup(db_path, terms, limit, tenant_id=tenant_id)
        if skill_result is not None:
            return skill_result

    # Phase 2: Cache check
    cache_key = (
        make_cache_key(
            db_path,
            fts_query,
            limit,
            rerank,
            boost_pinned,
            recency_weight,
            include_invalid,
            include_global,
        )
        + f":sw={int(safety_wiring)}:dr={int(deep_rerank)}:sf={int(skill_first)}"
        + f":if={int(include_facts)}:fl={int(fact_limit)}"
        + f":as_of={as_of}"
        + f":bs={belief_status or ''}:es={epistemic_source or ''}:ft={fact_type or ''}:ms={memory_source or ''}"
        + (f":tags={','.join(sorted(tags))}" if tags else "")
        + f":swm={int(shared_with_me)}"
        + f":tid={tenant_id}"
    )
    from infra.cache import cache_touch

    now = time.time()
    with _search_cache_lock:
        if cache_key in _search_cache:
            ts, cached_result = _search_cache[cache_key]
            if not SEARCH_CACHE_TTL_ENABLED or now - ts <= SEARCH_CACHE_TTL:
                cache_touch(cache_key)
                cached_result = dict(cached_result)
                cached_result["query_id"] = uuid.uuid4().hex
                return cached_result
            _search_cache.pop(cache_key)

    db = None
    try:
        from infra._lazy_imports import connection_pool

        db = connection_pool.get(str(db_path), timeout=30.0, tenant_id=tenant_id)
        _effective_rerank = rerank and not light

        # Phase 3: DB setup
        cols = _get_memories_columns(db)
        has_fitness = "fitness_score" in cols
        repo_filter = ""
        # Apply thread-local agent namespace scoping to the SQL search query.
        try:
            from agent_context import get_agent

            ctx = get_agent()
            if ctx.namespace != "default" and ctx.namespace is not None:
                if include_global:
                    repo_filter = f" AND (m.source_file LIKE 'agents/{ctx.namespace}/%' OR m.source_file NOT LIKE 'agents/%')"
                else:
                    repo_filter = f" AND m.source_file LIKE 'agents/{ctx.namespace}/%'"
        except (ImportError, AttributeError):
            pass
        # Sprint 2: memory_source filter (agent / auto_save / import)
        if memory_source is not None:
            source_map = {
                "agent": "m.source_file LIKE 'agents/%' OR m.source_file LIKE 'lessons/%'",
                "auto_save": "m.source_file LIKE 'sessions/auto%'",
                "import": "m.source_file LIKE 'imported/%'",
            }
            clause = source_map.get(memory_source)
            if clause:
                repo_filter = f"{repo_filter} AND ({clause})" if repo_filter else f" AND ({clause})"

        # Phase 1a: default category bias — exclude noisy auto-save session
        # transcripts from recall unless the caller explicitly requests a
        # category. The agent can opt back in via category='sessions' or
        # memory_source='auto_save'. The constraint is appended to
        # repo_filter so both FTS and embedding fallback paths inherit it
        # through _fetch_rows_by_ids.
        if category:
            if not re.match(r'^[A-Za-z0-9_-]+$', category):
                category = "lessons"
            repo_filter = f"{repo_filter} AND m.category = ?"
        else:
            repo_filter = f"{repo_filter} AND (m.category IS NULL OR m.category != 'sessions')"

        # Sprint 3: tags filter — JSON array exact match via LIKE.
        # Parameterised to prevent SQL injection (was: f-string interpolation
        # of user-supplied tag strings directly into the SQL clause).
        _tag_filter_clauses: list[str] = []
        _tag_filter_params: list[str] = []
        if tags:
            safe_tags = [re.sub(r'[^\w@.#+\-]', '', t) for t in tags]
            safe_tags = [t for t in safe_tags if t]
            for t in safe_tags:
                _tag_filter_clauses.append("m.tags LIKE ?")
                _tag_filter_params.append(f'%"{t}"%')
        _tag_filter_sql = ""
        if _tag_filter_clauses:
            _tag_filter_sql = " AND (" + " AND ".join(_tag_filter_clauses) + ")"

        # Phase 4 + Phase 4b: FTS + KG fact search
        # When search_parallel_enabled is on (default), run FTS and KG fact
        # lookup concurrently — they hit different tables and are independent.
        # When off (or if the feature flag is unavailable), fall back to the
        # original sequential order.
        _t0 = time.time()
        results: list[Any] = []
        related_facts: list[dict] = []
        _search_parallel: bool = False
        try:
            from infra._lazy_imports import get_config
            _search_parallel = bool(getattr(get_config(), "search_parallel_enabled", True))
        except (ImportError, AttributeError):
            _search_parallel = True

        if _search_parallel and include_facts:
            def _fts_worker() -> list:
                conn = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
                try:
                    return _fts_search(
                        conn, fts_query,
                        limit * 10 if _effective_rerank else limit,
                        has_fitness, repo_filter,
                        tag_filter_sql=_tag_filter_sql,
                        tag_filter_params=tuple(_tag_filter_params),
                        category=category or None,
                    )
                except Exception as _fts_exc:
                    _phase_inc("search.fts", _fts_exc)
                    logger.warning("fts_worker failed: %s", _fts_exc)
                    return []
                finally:
                    connection_pool.put(conn)

            def _kg_worker() -> list:
                conn = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
                try:
                    return _search_kg_facts(
                        conn, fts_query, fact_limit, include_invalid,
                        as_of=as_of,
                        belief_status=belief_status,
                        epistemic_source=epistemic_source,
                        fact_type=fact_type,
                    )
                except Exception as _kg_exc:
                    _phase_inc("search.kg_facts", _kg_exc)
                    logger.warning("kg_worker failed: %s", _kg_exc)
                    return []
                finally:
                    connection_pool.put(conn)

            with ThreadPoolExecutor(max_workers=2) as executor:
                fts_future = executor.submit(_fts_worker)
                kg_future = executor.submit(_kg_worker)
                results = fts_future.result()
                _record_phase_latency("search.fts", _t0)
                related_facts = kg_future.result()
                _record_phase_latency("search.kg_facts", _t0)
        else:
            results = _fts_search(
                db, fts_query, limit * 5 if _effective_rerank else limit, has_fitness,
                repo_filter,
                tag_filter_sql=_tag_filter_sql,
                tag_filter_params=tuple(_tag_filter_params),
                category=category or None,
            )
            _record_phase_latency("search.fts", _t0)
            if include_facts:
                _t0_kg = time.time()
                related_facts = _search_kg_facts(
                    db, fts_query, fact_limit, include_invalid,
                    as_of=as_of,
                    belief_status=belief_status,
                    epistemic_source=epistemic_source,
                    fact_type=fact_type,
                )
                _record_phase_latency("search.kg_facts", _t0_kg)

        # Phase 5: Fallback to embeddings
        if not results:
            _is_opaque = bool(re.fullmatch(r"[A-Za-z0-9_\-]{6,}", query or ""))
            if not _is_opaque:
                import search_pipeline
                _t0 = time.time()
                results = search_pipeline._fallback_embedding_search(
                    db, normalized_query, db_path, limit, repo_filter, category,
                    tag_filter_sql=_tag_filter_sql, tag_filter_params=tuple(_tag_filter_params),
                )
                _record_phase_latency("search.embedding_fallback", _t0)
            if not results:
                try:
                    total = db.execute("SELECT COUNT(*) FROM tenant_memories").fetchone()[0]
                except sqlite3.Error:
                    total = 0
                if total == 0:
                    hint = "The database is empty."
                elif _is_opaque:
                    hint = (
                        "FTS5 returned no exact matches for this opaque token. "
                        "Embedding fallback was skipped (queries that look like "
                        "slugs/IDs have no useful semantic neighbours)."
                    )
                else:
                    hint = "FTS5 and embedding fallback both returned no results."
                return _build_empty_result_with_hint(
                    cache_key=cache_key,
                    query=query,
                    db_path=db_path,
                    hint=hint,
                    related_facts=related_facts if include_facts else None,
                )

        # Phase 6: Hybrid fusion — always run embedding search as parallel
        # candidate source. With bge-large-en-v1.5, semantic search finds
        # relevant results that FTS misses due to vocabulary mismatch.
        if results:
            _t0 = time.time()
            results = _hybrid_fusion(
                db, results, normalized_query, fts_query, db_path, limit, repo_filter, category=category or None,
            )
            _record_phase_latency("search.hybrid_fusion", _t0)

        # Phase 7: Temporal filtering
        if not include_invalid or as_of is not None:
            if "valid_to" in cols:
                if as_of is not None:
                    as_of_iso = time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.gmtime(as_of)
                    )
                    if "valid_from" in cols:
                        valid_ids = {
                            row[0]
                            for row in db.execute(
                                "SELECT id FROM tenant_memories "
                                "WHERE (valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
                                "AND (valid_to IS NULL OR valid_to = '' OR valid_to > ?)",
                                (as_of_iso, as_of_iso),
                            ).fetchall()
                        }
                    else:
                        valid_ids = {
                            row[0]
                            for row in db.execute(
                                "SELECT id FROM tenant_memories "
                                "WHERE valid_to IS NULL OR valid_to = '' OR valid_to >= ?",
                                (as_of_iso,),
                            ).fetchall()
                        }
                else:
                    valid_ids = {
                        row[0]
                        for row in db.execute(
                            "SELECT id FROM tenant_memories WHERE valid_to IS NULL OR valid_to = ''"
                        ).fetchall()
                    }
                results = [r for r in results if r[0] in valid_ids]
                if not results:
                    return _build_empty_result_with_hint(
                        cache_key=cache_key,
                        query=f"{query} (after temporal filter)",
                        db_path=db_path,
                        hint=None,
                        related_facts=related_facts if include_facts else None,
                    )

        # Phase 8: Chunk enhancement
        results = _enhance_with_chunks(
            db, results, fts_query, limit, include_invalid, repo_filter, category=category or None,
        )

        # Phase 9: Reranking
        _t0 = time.time()
        try:
            results_to_display, _search_ctr_weights = _rerank_results(
                results=results,
                query=query,
                db_path=db_path,
                has_fitness=has_fitness,
                rerank=_effective_rerank,
                boost_pinned=boost_pinned if not light else False,
                recency_weight=recency_weight,
                limit=limit,
                deep_rerank=deep_rerank,
                as_of=as_of,
            )
        except Exception as _rerank_exc:
            _phase_inc("search.rerank", _rerank_exc)
            logger.warning(
                "rerank degraded (falling back to FTS-ranked results): %s", _rerank_exc
            )
            _search_ctr_weights = None
            if has_fitness and _effective_rerank:
                results_to_display = [
                    (
                        r[0], r[1], r[2], r[3], r[4], r[5],
                        -r[5], None, None, None,
                        r[9] if len(r) > 9 else None,
                        r[10] if len(r) > 10 else None,
                        r[11] if len(r) > 11 else 1,
                        None,
                    )
                    for r in results
                ]
            else:
                results_to_display = list(results)
        _record_phase_latency("rerank", _t0)

        # Phase 10: Build output
        result_items, output, backlinks_map = _build_result_items(
            db=db,
            results_to_display=results_to_display,
            query=query,
            rerank=rerank,
            db_path=db_path,
            as_of=as_of,
        )

        # Phase 11: Safety demoting
        if not light:
            if safety_wiring and result_items:
                result_items, output, results_to_display = _apply_safety_demoting(
                    result_items, output, results_to_display
                )

        # Phase 11b: Quality gates
        if not light:
            result_items, output, results_to_display = _apply_quality_gates(
                result_items=result_items,
                output=output,
                results_to_display=results_to_display,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
            )

        # Phase 11c: User profiling
        if not light:
            result_items, output, results_to_display = _apply_user_profiling(
                result_items=result_items,
                output=output,
                results_to_display=results_to_display,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
                db_path=db_path,
            )

        # QB6 (final pass)
        if not light:
            result_items, output, results_to_display = _apply_strong_match_boost(
                result_items=result_items,
                output=output,
                results_to_display=results_to_display,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
            )

        # Save-then-search atomicity hint
        if not light:
            result_items, output, results_to_display = _apply_save_hint_floater(
                db=db,
                db_path=db_path,
                result_items=result_items,
                output=output,
                results_to_display=results_to_display,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
            )

        # Phase 12: Record access
        _record_last_accessed(db, result_items)

        # B3.1: shared_with_me post-filter — append shared memories whose
        # source_note_id matches a result and target_agent_id is the
        # current agent, de-duplicating by id.
        if shared_with_me:
            _swm_t0 = time.time()
            try:
                from infra._lazy_imports import get_agent as _swm_get_agent
                _swm_agent_id = _swm_get_agent().agent_id
            except (ImportError, AttributeError):
                _swm_agent_id = None
            if _swm_agent_id:
                _seen_ids = {r[0] for r in results_to_display}
                try:
                    _swm_rows = db.execute(
                        "SELECT source_note_id FROM shared_memories "
                        "WHERE target_agent_id = ? AND source_note_id IS NOT NULL",
                        (_swm_agent_id,),
                    ).fetchall()
                    _swm_source_ids = {r[0] for r in _swm_rows}
                    _new_ids = _swm_source_ids - _seen_ids
                    if _new_ids:
                        _swm_extra = db.execute(
                            f"SELECT id, content, source_file, tags, created_at, "
                            f"importance, category, fitness_score, last_accessed, "
                            f"metadata "
                            f"FROM tenant_memories WHERE id IN ({','.join('?'*len(_new_ids))})",
                            tuple(_new_ids),
                        ).fetchall()
                        if _swm_extra:
                            results_to_display = list(results_to_display) + list(_swm_extra)
                except Exception as _swm_exc:
                    _phase_inc("search.shared_with_me", _swm_exc)
                    logger.warning("shared_with_me filter failed: %s", _swm_exc)
            _record_phase_latency("shared_with_me", _swm_t0)

        # B3.2: Cross-namespace audit logging — fires after search completes
        # when include_global=True and the calling agent is NOT the default namespace.
        _ns_audit_t0 = time.time()
        if include_global:
            try:
                from infra._lazy_imports import get_agent as _ns_audit_agent
                _ns_ctx = _ns_audit_agent()
                if _ns_ctx.namespace not in (None, "default"):
                    try:
                        from infra.audit import enqueue_audit as _ns_enqueue
                        _ns_enqueue(
                            db_path=str(db_path),
                            tool="memory_search",
                            args={
                                "query": query,
                                "include_global": True,
                                "agent_namespace": _ns_ctx.namespace,
                                "shared_with_me": shared_with_me,
                            },
                            results_count=len(result_items),
                            latency_ms=(time.time() - _ns_audit_t0) * 1000.0,
                        )
                    except Exception as _ns_audit_exc:
                        logger.warning("namespace audit enqueue failed: %s", _ns_audit_exc)
            except (ImportError, AttributeError):
                pass
        _record_phase_latency("namespace_audit", _ns_audit_t0)

        result = _build_search_result_envelope(
            result_items=result_items,
            output=output,
            results_to_display=results_to_display,
            synthesize=synthesize,
            query=query,
            max_synthesis_sentences=max_synthesis_sentences,
            related_facts=related_facts if include_facts else None,
        )
        _cache_store_result(cache_key, result)
        _phase_errs = _phase_counts()
        if _phase_errs.get("total_count"):
            result["phase_errors"] = _phase_errs
        if _phase_latencies:
            result["phase_latencies"] = dict(_phase_latencies)
        _record_search_telemetry(
            db=db,
            query_id=result["query_id"],
            result_items=result_items,
            ctr_weights=_search_ctr_weights,
        )
        _record_search_phase_latencies(
            db=db,
            query_id=result["query_id"],
            phase_latencies=dict(_phase_latencies),
        )
        return result
    except Exception as e:
        _phase_inc("search.orchestrator", e)
        logger.warning("search_memories failed: %s", e)
        return {
            "results": [],
            "count": 0,
            "output": _err(ErrorCode.DB_ERROR, f"Search failed: {e}"),
        }
    finally:
        if db is not None:
            try:
                safe_close_db(db)
            except Exception as e:
                logger.warning("safe_close_db failed: %s", e)


# Backward-compatible phase latency helper for test_observability.py.
def _record_phase_latency(name: str, start_time: float) -> None:
    """Record elapsed wall-clock latency for *name* into _phase_latencies."""
    elapsed_ms = (time.time() - start_time) * 1000.0
    with _phase_latencies_lock:
        _phase_latencies[name] = elapsed_ms
