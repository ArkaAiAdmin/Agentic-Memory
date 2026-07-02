from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

from infra.cache import (
    _search_cache,
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
from infra.error_counter import increment as _phase_inc, get_counts as _phase_counts
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
    _apply_cross_encoder_rerank,
    _apply_late_interaction_rerank,
)
from search.scoring import (
    _reciprocal_rank_fusion,
    _apply_temporal_decay,
    _apply_neural_forget_curve,
    _strong_match_float,
    _compute_final_score,
    compute_channel_weights,
    _sp_lazy,
)
from search.synthesis import (
    _bb1_synthesize,
)

logger = logging.getLogger(__name__)

_db_columns_cache: dict = {}
_db_columns_cache_lock = threading.Lock()

# Backward-compatible phase latency tracking (pre-error_counter API).
_phase_latencies: dict[str, float] = {}
_phase_latencies_lock = threading.Lock()


def _get_memories_columns(db: AnyConnection) -> set[str]:
    """Cache memories table columns by DB path to save PRAGMA queries."""
    try:
        db_path_row = db.execute("PRAGMA database_list").fetchone()
        db_path = db_path_row[2] if db_path_row is not None else ""
    except Exception:
        db_path = ""

    if not db_path:
        try:
            return {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()
            }
        except Exception:
            return set()

    with _db_columns_cache_lock:
        cols = _db_columns_cache.get(db_path)

    if cols is None:
        try:
            cols = {
                row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()
            }
            with _db_columns_cache_lock:
                _db_columns_cache[db_path] = cols
        except Exception:
            cols = set()

    return cols


# Only allow safe SQL fragments in extra_filter to prevent injection.
# Safe characters: spaces, alphanumeric, SQL punctuation (AND/OR/NOT/=, etc.)
# Ban semicolons to prevent multi-statement injection.
_SQL_SAFE_FILTER_RE = re.compile(r"^[ A-Za-z0-9_.,=<>!()'\"%\-/]+$")


def _fetch_rows_by_ids(
    db: AnyConnection,
    ids: list,
    table: str = "memories",
    columns: str = "id, content, source_file, tags, created_at, fitness_score, importance, pinned, last_accessed, metadata, access_count",
    extra_filter: str = "",
) -> dict:
    """Batch-fetch rows by IDs to avoid N+1 queries. Returns {id: row_tuple}.

    Chunks at 500 IDs per query to stay under SQLite's ~999 variable limit.
    """
    if not ids:
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
            rows = db.execute(query, chunk).fetchall()
            result.update({row[0]: row for row in rows})
        except Exception:
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
    except Exception:
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
    except Exception:
        return True


def _get_embedding_score_threshold() -> float:
    try:
        from infra._lazy_imports import get_config

        return float(get_config().embedding_score_threshold)
    except Exception:
        return 0.25


# Cache for skill-first lookups to prevent double-incrementing hit_count
# Bounded LRU via OrderedDict; evicts oldest entries past MAX_SKILL_CACHE.
_SKILL_CACHE_MAX = 512
_skill_cache: dict = {}
_skill_cache_order: list = []


def _skill_first_lookup(db_path: Path, terms: list[str], limit: int, tenant_id: str = "default") -> dict | None:
    """Look up skills in memory_skills table matching the query terms."""
    cache_key = (str(db_path), tuple(sorted(terms)), limit)
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]  # type: ignore[no-any-return]

    try:
        from infra._lazy_imports import connection_pool, safe_close_db

        db = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
    except Exception as exc:
        logger.warning("_skill_first_lookup: connection_pool.get failed: %s", exc)
        return None
    try:
        # Check if memory_skills table exists
        try:
            table_check = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_skills'"
            ).fetchone()
        except Exception:
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
        except Exception:
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
        except Exception:
            pass

        # Format results
        results = []
        for row in rows:
            results.append(
                {
                    "id": row[1],
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
        _skill_cache[cache_key] = result
        _skill_cache_order.append(cache_key)
        if len(_skill_cache) > _SKILL_CACHE_MAX:
            _oldest = _skill_cache_order.pop(0)
            _skill_cache.pop(_oldest, None)
        return result
    finally:
        try:
            safe_close_db(db)
        except Exception:
            pass


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
    """Record CTR feedback with connection lifecycle managed."""
    import time as _time

    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        now = returned_at if returned_at is not None else _time.time()
        if action == "returned":
            conn.execute(
                "INSERT OR REPLACE INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source, ranking_params) "
                "VALUES (?, ?, ?, ?, ?)",
                (id, query_id, now, source, ranking_params),
            )
        elif action == "clicked":
            conn.execute(
                "UPDATE memory_ctr_feedback SET clicked_at = ? "
                "WHERE id = ? AND query_id = ?",
                (now, id, query_id),
            )
        elif action == "dismissed":
            conn.execute(
                "UPDATE memory_ctr_feedback SET dismissed_at = ? "
                "WHERE id = ? AND query_id = ?",
                (now, id, query_id),
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
            except Exception:
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
            except Exception:
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
        embeddings = _np.stack(vectors)
        centroid = embeddings.mean(axis=0)
        prev = conn.execute(
            "SELECT drifted_dimensions FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
        ).fetchone()
        prev_centroid = None
        if prev and prev[0]:
            try:
                prev_centroid = _np.array(json.loads(prev[0]))
            except Exception:
                pass
        if prev_centroid is not None and len(prev_centroid) == len(centroid):
            cos_sim = float(
                _np.dot(centroid, prev_centroid)
                / (_np.linalg.norm(centroid) * _np.linalg.norm(prev_centroid) + 1e-10)
            )
            drift = 1.0 - cos_sim
        else:
            drift = 0.0
        diff = centroid - (
            prev_centroid if prev_centroid is not None else _np.zeros_like(centroid)
        )
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
    """T10 + Sprint 5 + Sprint 1: search the knowledge-graph fact index.

    Surfaces structured facts (``subject --[predicate]--> object``) alongside
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

    Returns a list of dicts sorted by FTS5 BM25 rank (best first).  Returns
    an empty list if the kg_facts table is missing or empty — never raises.
    """
    try:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_facts'"
        ).fetchone():
            return []
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
                f" AND (kf.superseded_by IS NULL)"
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
        rows = db.execute(
            "SELECT kf.id, kf.subject, kf.predicate, kf.object, kf.confidence, "
            "kf.mention_count, kf.first_seen, kf.last_seen, kf.event_time, "
            "kf.event_time_granularity, kf.contradiction_score, kg_facts_fts.rank, "
            "kf.belief_status, kf.epistemic_source, kf.fact_type "
            "FROM kg_facts_fts "
            "JOIN kg_facts kf ON kf.rowid = kg_facts_fts.rowid "
            f"WHERE kg_facts_fts MATCH ?{invalid_filter}{belief_filter} "
            "ORDER BY kg_facts_fts.rank "
            "LIMIT ?",
            (fts_query, *belief_params, limit),
        ).fetchall()
    except Exception:
        logger.warning("KG fact search failed; returning empty list", exc_info=True)
        return []

    results = []
    for r in rows:
        results.append(
            {
                "id": r[0],
                "subject": r[1],
                "predicate": r[2],
                "object": r[3],
                "confidence": r[4],
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
        )
    return results


def _fts_search(
    db: AnyConnection,
    fts_query: str,
    limit: int,
    has_fitness: bool,
    repo_filter: str,
) -> list:
    """Execute FTS5 search and return raw result rows."""
    if has_fitness:
        return db.execute(
            f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
            "                 m.fitness_score, m.importance, m.pinned, m.last_accessed, m.metadata, m.access_count\n"
            "          FROM memories_fts fts\n"
            "          JOIN memories m ON m.rowid = fts.rowid\n"
            f"          WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{repo_filter}\n"
            "          ORDER BY fts.rank\n"
            "          LIMIT ?",
            (fts_query, limit * 3),
        ).fetchall()
    return db.execute(
        f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
        "             NULL, NULL, NULL, m.last_accessed, m.metadata, m.access_count\n"
        "      FROM memories_fts fts\n"
        "      JOIN memories m ON m.rowid = fts.rowid\n"
        f"      WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{repo_filter}\n"
        "      ORDER BY fts.rank\n"
        "      LIMIT ?",
        (fts_query, limit),
    ).fetchall()


def _fallback_embedding_search(
    db: AnyConnection,
    normalized_query: str,
    db_path: Path,
    limit: int,
    repo_filter: str,
) -> list:
    """Try embedding search as fallback when FTS returns nothing."""
    try:
        from infra._lazy_imports import get_embedding_search

        _es = get_embedding_search()
        _es_results = _es.search(normalized_query, db_path, limit=limit * 2)
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
        rows_map = _fetch_rows_by_ids(db, hit_ids, extra_filter=repo_filter)

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
    except Exception:
        return []


def _hybrid_fusion(
    db: AnyConnection,
    results: list,
    normalized_query: str,
    db_path: Path,
    limit: int,
    repo_filter: str,
) -> list:
    """Merge FTS and semantic results using reciprocal rank fusion."""
    try:
        from infra._lazy_imports import get_config, get_embedding_search

        _es = get_embedding_search()
        _overfetch = int(getattr(get_config(), "hybrid_semantic_overfetch", 3))
        _rrf_k = int(getattr(get_config(), "hybrid_rrf_k", 60))
        _rank_scale = float(getattr(get_config(), "hybrid_rank_proxy_scale", 30.0))
        _fts_w = float(getattr(get_config(), "hybrid_fts_weight", 1.0))
        _sem_w = float(getattr(get_config(), "hybrid_semantic_weight", 1.0))
        _es_results = _es.search(normalized_query, db_path, limit=limit * _overfetch)
        if not isinstance(_es_results, list) or not _es_results:
            return results
        fts_ranked = [r[0] for r in results]
        sem_ranked = [h.get("id") for h in _es_results if h.get("id")]
        rrf = _reciprocal_rank_fusion(
            [fts_ranked, sem_ranked], k=_rrf_k, weights=[_fts_w, _sem_w]
        )
        existing_ids = {r[0]: i for i, r in enumerate(results)}
        new_hit_ids = [
            h.get("id")
            for h in _es_results
            if h.get("id") and h.get("id") not in existing_ids
        ]
        new_hit_rows = _fetch_rows_by_ids(db, new_hit_ids, extra_filter=repo_filter)
        semantic_only = []
        for hit in _es_results:
            hit_id = hit.get("id")
            if not hit_id or hit_id in existing_ids:
                continue
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
) -> list:
    """Add chunk-level matches to results."""
    try:
        chunk_hits = _search_chunks_enhanced(db, fts_query, limit=limit * 2)
        if not chunk_hits:
            return results
        merged = _merge_chunk_hits(chunk_hits)
        seen_ids = {r[0] for r in results}
        chunk_parent_ids = [p_id for p_id, _, _, _, _ in merged if p_id not in seen_ids]
        chunk_rows = _fetch_rows_by_ids(db, chunk_parent_ids, extra_filter=repo_filter)
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
    except Exception:
        pass
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
        metadata_json = r[10] if len(r) > 10 else None
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
    results,
    query,
    db_path,
    has_fitness,
    rerank,
    boost_pinned,
    recency_weight,
    limit,
    deep_rerank,
    as_of: float | None = None,
):
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
                )
            )
        if _sp_lazy("_FORGETTING_CURVE_ENABLED", False):
            return _apply_neural_forget_curve(out, query), None
        return _apply_temporal_decay(out, as_of=as_of), None

    _qtype = _detect_query_type(query)
    _qweights = _weights_for_query_type(_qtype)
    _ctr_w = compute_channel_weights(db_path)
    if _ctr_w is not None:
        _qweights = _ctr_w
    scored = []
    for r in results:
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
            )
        )

    scored = _strong_match_float(scored)
    out = _apply_cross_encoder_rerank(
        query,
        scored,
        top_k=min(len(scored), limit * 2),
        deep_rerank=deep_rerank,
    )
    out = _apply_late_interaction_rerank(query, out, top_k=min(len(out), limit * 2))
    if _sp_lazy("_FORGETTING_CURVE_ENABLED", False):
        out = _apply_neural_forget_curve(out, query)
    else:
        out = _apply_temporal_decay(out, as_of=as_of)
    return out[:limit], _qweights


def _build_result_items(*, db, results_to_display, query, rerank):
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
        except Exception:
            pass
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
        metadata_json = r[10] if len(r) > 10 else None
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        backlinks = backlinks_map.get(note_id, [])
        auto_summary = None
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
                "summary": auto_summary,
            }
        )
    output = _format_search_results(
        results_to_display,
        query,
        rerank,
        result_items,
        backlinks_map,
    )
    return result_items, output, backlinks_map


def _apply_strong_match_boost(
    *, result_items, output, results_to_display, query, rerank, backlinks_map
):
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
        except Exception:
            pass
        return result_items, output, results_to_display
    except Exception:
        return result_items, output, results_to_display


def _cache_store_result(cache_key: str, result: dict) -> None:
    """Store a search result in the LRU cache and enforce the size cap.

    The 3-line "set + move_to_end + pop oldest" sequence appears in
    every code path that returns a result dict from search_memories.
    Centralizing it here keeps the cache-eviction policy in one place
    — if SEARCH_CACHE_MAX is ever changed (e.g. per-deployment tuning)
    this is the only spot to touch.
    """
    _search_cache[cache_key] = (time.time(), result)
    _search_cache.move_to_end(cache_key)
    if len(_search_cache) > SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)


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
                except Exception:
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
    }
    if related_facts:
        result["related_facts"] = related_facts
    _cache_store_result(cache_key, result)
    return result


def _record_last_accessed(db, result_items) -> None:
    """Phase 12 of search_memories: stamp last_accessed on every result row.

    Bumps ``last_accessed`` to the current ISO timestamp in a single
    batched UPDATE for every result note.  No-op if the result set is
    empty.  All exceptions are swallowed — adaptive retention depends
    on this column but a failure to record is non-fatal.
    """
    if not result_items:
        return
    try:
        import datetime as _dt

        now_iso = _dt.datetime.now().isoformat(timespec="seconds")
        placeholders = ",".join(("?" for _ in result_items))
        ids = [r["id"] for r in result_items]
        db.execute(
            f"UPDATE memories SET last_accessed = ? WHERE id IN ({placeholders})",
            [now_iso] + ids,
        )
        db.commit()
    except Exception as e:
        _phase_inc("search.record_last_accessed", e)
        logger.warning("_record_last_accessed failed: %s", e)


def _build_search_result_envelope(
    *,
    result_items,
    output,
    results_to_display,
    synthesize,
    query,
    max_synthesis_sentences,
    related_facts=None,
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
                except Exception:
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
        except Exception:
            pass
    return result


def _apply_quality_gates(
    *, result_items, output, results_to_display, query, rerank, backlinks_map
):
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
                output = _format_search_results(
                    [
                        r
                        for r in results_to_display
                        if r[0] in {ri["id"] for ri in result_items}
                    ],
                    query,
                    rerank,
                    result_items,
                    backlinks_map,
                )
    except Exception as e:
        _phase_inc("search.quality_gates", e)
        logger.warning("quality_gates failed: %s", e)
    return result_items, output


def _apply_user_profiling(
    *,
    result_items,
    output,
    results_to_display,
    query,
    rerank,
    backlinks_map,
    db_path,
):
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
            output = _format_search_results(
                [
                    r
                    for r in results_to_display
                    if r[0] in {ri["id"] for ri in result_items}
                ],
                query,
                rerank,
                result_items,
                backlinks_map,
            )
    except Exception as e:
        _phase_inc("search.user_profiling", e)
        logger.warning("user_profiling failed: %s", e)
    return result_items, output


def _record_search_telemetry(*, db, query_id, result_items, ctr_weights) -> None:
    """Record CTR feedback and adaptive-retention access events for the result set.

    Two side-effects, both best-effort:

    1. Write a row to ``memory_ctr_feedback`` so the next CTR computation
       can correlate this query's result set with user-click behavior.
    2. Call ``adaptive_retention.record_access`` for each result row so
       the per-note fitness decay respects the fresh access.

    All exceptions are swallowed — telemetry is informational, not a
    precondition for the user seeing results.
    """
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
        _phase_inc("search.record_search_telemetry", e)
        logger.warning("record_search_telemetry failed: %s", e)
        pass
    try:
        from adaptive_retention import record_access

        for r in result_items:
            record_access(db, r.get("id", ""), source="search")
        db.commit()
    except Exception as e:
        _phase_inc("search.record_search_telemetry", e)
        logger.warning("record_search_telemetry failed: %s", e)


def _apply_save_hint_floater(
    *, db, db_path, result_items, output, query, rerank, backlinks_map
):
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
    except Exception:
        return result_items, output
    if hint is None:
        return result_items, output
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
    except Exception:
        return result_items, output
    if not (hint_in_fts and result_items):
        return result_items, output
    if any(ri.get("id") == hint_id for ri in result_items):
        return result_items, output
    try:
        floater_row = db.execute(
            "SELECT id, content, source_file, tags, created_at, "
            "fitness_score, importance, pinned, last_accessed, "
            "metadata, valid_to, 0 AS rank, 0.5 AS final_score "
            "FROM tenant_memories WHERE id = ? AND deleted_at IS NULL",
            (hint_id,),
        ).fetchone()
    except Exception:
        return result_items, output
    if floater_row is None:
        return result_items, output
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
    try:
        output = _format_search_results(
            [floater_row + (0, 1.0)],
            query,
            rerank,
            result_items,
            backlinks_map,
        )
    except Exception:
        pass
    return result_items, output


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
) -> dict:
    if not db_path.exists():
        return {
            "results": [],
            "count": 0,
            "output": _err(
                ErrorCode.DB_ERROR,
                f"Memory database not found in current directory ({db_path}). Run memory_rebuild tool first.",
            ),
        }

    # Phase 1: Parse query
    _t0 = time.time()
    normalized_query, fts_query, bare_text, graph_rag_terms = _parse_search_query(
        query, db_path
    )
    _record_phase_latency("parse_query", _t0)
    terms = re.findall("[\\w@\\#\\.\\+\\-]+", fts_query, flags=re.UNICODE)
    if not terms:
        return {
            "results": [],
            "count": 0,
            "output": f"No memories matched the query: '{query}'",
            "suggestions": _build_zero_result_suggestions(db_path, query),
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
    )
    now = time.time()
    if cache_key in _search_cache:
        ts, cached_result = _search_cache[cache_key]
        if not SEARCH_CACHE_TTL_ENABLED or now - ts <= SEARCH_CACHE_TTL:
            _search_cache.move_to_end(cache_key)
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
        except (ImportError, Exception):
            pass
        # Sprint 2: memory_source filter (agent / auto_save / import)
        if memory_source is not None:
            source_map = {
                "agent": "m.source_file LIKE 'agents/%' OR m.source_file LIKE 'lessons/%'",
                "auto_save": "m.source_file LIKE 'auto_saves/%'",
                "import": "m.source_file LIKE 'imported/%'",
            }
            clause = source_map.get(memory_source)
            if clause:
                repo_filter = f"{repo_filter} AND ({clause})" if repo_filter else f" AND ({clause})"

        # Phase 4: FTS search
        _t0 = time.time()
        results = _fts_search(
            db, fts_query, limit * 3 if _effective_rerank else limit, has_fitness, repo_filter
        )
        _record_phase_latency("fts", _t0)

        # Phase 4b: T10 — KG fact search (independent of memory results).
        # Facts are surfaced in the output as a "Related facts" section and
        # included in the result envelope.  Failure to find any facts is not
        # an error — it's the common case (KG may be disabled, or no facts
        # match the query).  We always run this regardless of whether
        # `results` is empty so users with no memory hits can still see
        # matching facts.
        related_facts: list[dict] = []
        if include_facts:
            _t0 = time.time()
            related_facts = _search_kg_facts(
                db, fts_query, fact_limit, include_invalid,
                as_of=as_of,
                belief_status=belief_status,
                epistemic_source=epistemic_source,
                fact_type=fact_type,
            )
            _record_phase_latency("kg_facts", _t0)

        # Phase 5: Fallback to embeddings
        if not results:
            _is_opaque = bool(re.fullmatch(r"[A-Za-z0-9_\-]{6,}", query or ""))
            if not _is_opaque:
                import search_pipeline
                _t0 = time.time()
                results = search_pipeline._fallback_embedding_search(
                    db, normalized_query, db_path, limit, repo_filter
                )
                _record_phase_latency("embedding_fallback", _t0)
            if not results:
                try:
                    total = db.execute("SELECT COUNT(*) FROM tenant_memories").fetchone()[0]
                except Exception:
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

        # Phase 6: Hybrid fusion
        if hybrid and results:
            _t0 = time.time()
            results = _hybrid_fusion(
                db, results, normalized_query, db_path, limit, repo_filter
            )
            _record_phase_latency("hybrid_fusion", _t0)

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
            db, results, fts_query, limit, include_invalid, repo_filter
        )

        # Phase 9: Reranking
        _t0 = time.time()
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
        _record_phase_latency("rerank", _t0)

        # Phase 10: Build output
        result_items, output, backlinks_map = _build_result_items(
            db=db,
            results_to_display=results_to_display,
            query=query,
            rerank=rerank,
        )

        # Phase 11: Safety demoting
        if not light:
            if safety_wiring and result_items:
                result_items, output, results_to_display = _apply_safety_demoting(
                    result_items, output, results_to_display
                )

        # Phase 11b: Quality gates
        if not light:
            result_items, output = _apply_quality_gates(
                result_items=result_items,
                output=output,
                results_to_display=results_to_display,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
            )

        # Phase 11c: User profiling
        if not light:
            result_items, output = _apply_user_profiling(
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
            result_items, output = _apply_save_hint_floater(
                db=db,
                db_path=db_path,
                result_items=result_items,
                output=output,
                query=query,
                rerank=rerank,
                backlinks_map=backlinks_map,
            )

        # Phase 12: Record access
        _record_last_accessed(db, result_items)

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
        return result
    except Exception as e:
        _phase_inc("search.orchestrator", e)
        return {
            "results": [],
            "count": 0,
            "output": _err(ErrorCode.DB_ERROR, f"Search failed: {e}"),
        }
    finally:
        if db is not None:
            try:
                safe_close_db(db)
            except Exception:
                pass


# Backward-compatible phase latency helper for test_observability.py.
def _record_phase_latency(name: str, start_time: float) -> None:
    """Record elapsed wall-clock latency for *name* into _phase_latencies."""
    elapsed_ms = (time.time() - start_time) * 1000.0
    with _phase_latencies_lock:
        _phase_latencies[name] = elapsed_ms
