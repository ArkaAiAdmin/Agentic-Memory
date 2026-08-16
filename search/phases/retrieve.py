"""Phase 5–6 Retrieval functions: FTS5 BM25 search, embedding fallback, KG facts search, reasoning expand."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from search.config import get_search_config
from search.phases._db_utils import _fetch_rows_by_ids

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def _get_embedding_score_threshold() -> float:
    return get_search_config().embedding_score_threshold


def _fts_search(
    db: AnyConnection,
    fts_query: str,
    limit: int,
    has_fitness: bool,
    repo_filter: str = "",
    tag_filter_sql: str = "",
    tag_filter_params: tuple = (),
    category: str | None = None,
    prefilter_ids: set[str] | None = None,
    recency_order: bool = False,
) -> list:
    _base_filter = repo_filter + tag_filter_sql

    if prefilter_ids:
        _id_list = ",".join("?" for _ in prefilter_ids)
        _base_filter = _base_filter + f" AND m.id IN ({_id_list})"
        _params = tuple(prefilter_ids)
    else:
        _params = ()
    _order = "m.observed_at DESC" if recency_order else "fts.rank"
    params = (fts_query,) + tag_filter_params + _params
    # M5 fix: select 13 columns matching the canonical tuple shape
    # (id, content, source_file, tags, created, rank, fitness, importance,
    #  pinned, last_accessed, metadata, access_count, score).
    # Removed m.supersedes — accessed defensively at r[13] but always None
    # for FTS results; canonical 13-col form already handles it.
    res = db.execute(
        f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
        f"             {'m.fitness_score, m.importance, m.pinned' if has_fitness else 'NULL, NULL, NULL'}, m.last_accessed, m.metadata, m.access_count,\n"
        "             m.score\n"
        "      FROM memories_fts fts\n"
        "      JOIN tenant_memories m ON m.id = fts.id\n"
        f"      WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{_base_filter}\n"
        f"      ORDER BY {_order}\n"
        "      LIMIT ?",
        (*params, limit * 2),
    ).fetchall()
    if not res and tag_filter_sql:
        _base_filter_fallback = repo_filter + (f" AND m.id IN ({','.join('?' for _ in prefilter_ids)})" if prefilter_ids else "")
        _params_fallback = (fts_query,) + _params
        res = db.execute(
            f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, fts.rank,\n"
            f"             {'m.fitness_score, m.importance, m.pinned' if has_fitness else 'NULL, NULL, NULL'}, m.last_accessed, m.metadata, m.access_count,\n"
            "             m.score\n"
            "      FROM memories_fts fts\n"
            "      JOIN tenant_memories m ON m.id = fts.id\n"
            f"      WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{_base_filter_fallback}\n"
            f"      ORDER BY {_order}\n"
            "      LIMIT ?",
            (*_params_fallback, limit * 2),
        ).fetchall()
    return res


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
        _es_results = _es.search(normalized_query, db_path, limit=limit * 10, category=category)
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
        _cat_params = (category,) if (category and "m.category = ?" in _base_filter) else ()
        _params = _cat_params + tag_filter_params
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
            access_count = row[10] if len(row) > 10 else 1
            forget_score = row[11] if len(row) > 11 else None
            es_score = float(hit.get("score", 0.0))
            rank = -es_score
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
                    access_count,
                    forget_score,
                )
            )
        return fb_rows
    except Exception as e:
        logger.warning("_fallback_embedding_search failed: %s", e)
        return []


def _search_kg_facts(
    db: AnyConnection,
    fts_query: str,
    limit: int,
    include_invalid: bool,
    as_of: float | None = None,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    tenant_id: str = "default",
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
            f"WHERE kg_facts_fts MATCH ? AND kf.tenant_id = ?{invalid_filter}{belief_filter} "
            "ORDER BY kg_facts_fts.rank "
            "LIMIT ?",
            (fts_query, tenant_id, *belief_params, limit),
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


def _reasoning_expand(db_path: Path, query: str, limit: int = 5, conn=None, tenant_id: str = "default") -> list[str]:
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
        "is",
    )
    # Normalize query lower-case for predicate detection.
    q_lower = query.lower()
    matched_predicate: str | None = None
    for pred in _ENTAILMENT_PREDICATES:
        # Support both with and without internal underscores in the user query.
        readable = pred.replace("_", " ")
        if readable in q_lower:
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
    # M20 fix: escape % and _ in user-controlled tokens to prevent
    # unintended LIKE wildcard expansion.
    tokens = re.findall(r"[A-Za-z][A-Za-z\-_/]+", entity_term)
    filtered_tokens = [
        t.replace("%", "\\%").replace("_", "\\_")
        for t in tokens
        if len(t) > 2
    ]
    like_pattern = "%" + "%".join(filtered_tokens) + "%" if filtered_tokens else "%" + entity_term.replace("%", "\\%").replace("_", "\\_") + "%"
    _pooled_conn = None
    if conn is None:
        try:
            from infra._lazy_imports import connection_pool
            _pooled_conn = connection_pool.get(str(db_path), timeout=10.0)
            conn = _pooled_conn
        except Exception as exc:
            logger.warning("reasoning_expand connection failed: %s", exc)
            return []
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
              AND kf.tenant_id = ?
              AND (kf.subject LIKE ? ESCAPE '\\' OR kf.object LIKE ? ESCAPE '\\')
            LIMIT ?
            """,
            (tenant_id, like_pattern, like_pattern, limit),
        ).fetchall()
        return [row[0] for row in rows if row[0]]
    except Exception as exc:
        logger.warning("reasoning_expand failed: %s", exc)
        return []
    finally:
        if _pooled_conn is not None:
            try:
                from infra._lazy_imports import connection_pool
                connection_pool.put(_pooled_conn)
            except Exception as e:
                logger.warning("reasoning_expand: connection_pool.put failed: %s", e)
