"""Phase 7 & 9 Hybrid Fusion & Chunk Enhancement functions."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from infra.error_counter import increment as _phase_inc
from search.scoring import _reciprocal_rank_fusion
from search.phases._db_utils import _fetch_rows_by_ids, _get_memories_columns

if TYPE_CHECKING:
    from infra.db import AnyConnection

# Cached corpus size to avoid COUNT(*) on every hybrid search.
# Invalidated when db_path changes or after 60 seconds.
_corpus_size_cache: dict[str, tuple[float, int]] = {}
_CORPUS_CACHE_TTL = 60.0
_corpus_cache_lock = threading.Lock()

logger = logging.getLogger(__name__)


def clear_fusion_caches() -> None:
    """Clear corpus size cache."""
    with _corpus_cache_lock:
        _corpus_size_cache.clear()



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


def _hybrid_fusion(
    db: AnyConnection,
    results: list,
    normalized_query: str,
    fts_query: str,
    db_path: Path,
    limit: int,
    repo_filter: str,
    category: str | None = None,
    chunk_hits_out: list | None = None,
    embedding_weight_override: float | None = None,
) -> list:
    """Merge FTS, semantic, chunk FTS, and SPLADE results using RRF.

    FIX 4: when ``chunk_hits_out`` is provided (a list), the merged chunk
    FTS hits are appended to it so the caller can thread them into
    ``_enhance_with_chunks`` without re-querying the chunk index. This
    keeps the return type a flat result list (as callers/tests expect).
    """
    try:
        from infra._lazy_imports import get_config, get_embedding_search
        from search.budget_aware import compute_adaptive_overfetch

        _es = get_embedding_search()
        # Adaptive overfetch based on corpus size (cached to avoid COUNT(*) per query)
        import time as _time
        _cache_key = str(db_path) if db_path else "__none__"
        _now = _time.time()
        _corpus_size = 1000  # default
        try:
            if db is not None:
                with _corpus_cache_lock:
                    if _cache_key in _corpus_size_cache:
                        _cached_ts, _cached_size = _corpus_size_cache[_cache_key]
                        if _now - _cached_ts < _CORPUS_CACHE_TTL:
                            _corpus_size = _cached_size
                        else:
                            _row = db.execute("SELECT COUNT(*) FROM tenant_memories WHERE deleted_at IS NULL").fetchone()
                            _corpus_size = _row[0] if _row else 1000
                            _corpus_size_cache[_cache_key] = (_now, _corpus_size)
                    else:
                        _row = db.execute("SELECT COUNT(*) FROM tenant_memories WHERE deleted_at IS NULL").fetchone()
                        _corpus_size = _row[0] if _row else 1000
                        _corpus_size_cache[_cache_key] = (_now, _corpus_size)
        except Exception:
            _corpus_size = 1000
        from search.config import get_search_config
        _sc = get_search_config()
        _base_overfetch = int(getattr(get_config(), "hybrid_semantic_overfetch", 3))
        _overfetch = compute_adaptive_overfetch(_corpus_size, _base_overfetch)
        _rrf_k = int(getattr(_sc, "hybrid_rrf_k", 60))
        _rank_scale = float(getattr(_sc, "hybrid_rank_proxy_scale", 30.0))
        _fts_w = float(getattr(_sc, "hybrid_fts_weight", 1.0))
        _sem_w = float(getattr(_sc, "hybrid_semantic_weight", 1.0))
        if embedding_weight_override is not None:
            _sem_w = embedding_weight_override
        _chunk_fts_w = float(getattr(_sc, "hybrid_chunk_fts_weight", 0.8))
        _splade_w = float(getattr(_sc, "hybrid_splade_weight", 0.6))

        fts_ranked = [r[0] for r in results]

        def _do_vector() -> list:
            try:
                res = _es.search(normalized_query, db_path, limit=limit * _overfetch)
                return res if isinstance(res, list) else []
            except Exception:
                return []

        def _do_chunks() -> list:
            try:
                hits = _search_chunks_enhanced(db, fts_query, limit=limit * 2)
                return _merge_chunk_hits(hits)
            except Exception:
                return []

        def _do_splade() -> list:
            try:
                from infra.splade_encoder import encode_sparse
                from search.splade_index import splade_search

                query_sparse = encode_sparse(normalized_query)
                if query_sparse:
                    splade_results = splade_search(db, query_sparse, top_k=limit * _overfetch)
                    return [mid for mid, _ in splade_results]
            except Exception as _splade_exc:
                logger.debug("SPLADE search skipped: %s", _splade_exc)
            return []

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="hybrid-retrieval") as _tp:
            fut_v = _tp.submit(_do_vector)
            fut_c = _tp.submit(_do_chunks)
            fut_s = _tp.submit(_do_splade)
            _es_results = fut_v.result()
            merged_chunks = fut_c.result()
            splade_ranked = fut_s.result()

        sem_ranked = [h.get("id") for h in _es_results if h.get("id")]
        if chunk_hits_out is not None:
            chunk_hits_out.append(merged_chunks)
        chunk_fts_ranked = [p_id for p_id, _, _, _, _ in merged_chunks]


        # Adaptive weighting: boost semantic for abstract/synonym queries
        # Check if FTS has few results (indicates vocabulary mismatch)
        fts_count = len(fts_ranked)
        _sem_boost_threshold = int(getattr(_sc, "hybrid_sem_boost_threshold", 0))
        if _sem_boost_threshold > 0 and fts_count < _sem_boost_threshold:
            # FTS found few results — this is likely an abstract query
            # Boost semantic weight to find semantically similar content
            _sem_w = _sem_w * 2.0
            logger.debug("hybrid_fusion: boosting semantic weight for abstract query (fts=%d)", fts_count)

        # Fusion over Document FTS, Semantic, Chunk FTS, and SPLADE
        rrf = _reciprocal_rank_fusion(
            [fts_ranked, sem_ranked, chunk_fts_ranked, splade_ranked],
            k=_rrf_k,
            weights=[_fts_w, _sem_w, _chunk_fts_w, _splade_w]
        )

        # Single-channel rescue: documents appearing in only 1 channel
        # but ranking high there get a floor score so they aren't buried.
        # This is domain-agnostic — it boosts ANY high-ranking single-channel
        # hit, not just specific topics.
        _channel_presence: dict[str, int] = {}
        for ch_ranked in [fts_ranked, sem_ranked, chunk_fts_ranked, splade_ranked]:
            for rank, doc_id in enumerate(ch_ranked):
                if doc_id:
                    _channel_presence[doc_id] = _channel_presence.get(doc_id, 0) + 1
        _SINGLE_CHANNEL_FLOOR_RANK = 15  # top-N in a single channel
        _SINGLE_CHANNEL_FLOOR_SCORE = 0.008  # enough to surface in top-20
        for ch_ranked in [fts_ranked, sem_ranked, chunk_fts_ranked, splade_ranked]:
            for rank, doc_id in enumerate(ch_ranked):
                if doc_id and _channel_presence.get(doc_id, 0) == 1 and rank < _SINGLE_CHANNEL_FLOOR_RANK:
                    current = rrf.get(doc_id, 0.0)
                    if current < _SINGLE_CHANNEL_FLOOR_SCORE:
                        rrf[doc_id] = _SINGLE_CHANNEL_FLOOR_SCORE
                        logger.debug(
                            "single-channel rescue: %s rank=%d floor=%.4f",
                            doc_id, rank, _SINGLE_CHANNEL_FLOOR_SCORE,
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
        # Append RRF-based rank_proxy at a dedicated field (index 14) so
        # the original FTS rank at index 5 is preserved for downstream
        # consumers that rely on it.
        _RRF_FIELD_IDX = 14
        merged = []
        for r in results:
            hit_id = r[0]
            rrf_score = rrf.get(hit_id, 0.0)
            r_list = list(r)
            # Pad to _RRF_FIELD_IDX + 1 if needed
            while len(r_list) <= _RRF_FIELD_IDX:
                r_list.append(None)
            r_list[_RRF_FIELD_IDX] = -rrf_score * _rank_scale
            merged.append(tuple(r_list))
        for r in semantic_only:
            hit_id = r[0]
            rrf_score = rrf.get(hit_id, 0.0)
            r_list = list(r)
            while len(r_list) <= _RRF_FIELD_IDX:
                r_list.append(None)
            r_list[_RRF_FIELD_IDX] = -rrf_score * _rank_scale
            merged.append(tuple(r_list))
        merged.sort(
            key=lambda x: (
                x[_RRF_FIELD_IDX]
                if len(x) > _RRF_FIELD_IDX and x[_RRF_FIELD_IDX] is not None
                else (x[5] if len(x) > 5 and x[5] is not None else 0.0)
            )
        )
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
    merged_chunks: list | None = None,
) -> list:
    """Add chunk-level matches to results.

    FIX 4: when ``merged_chunks`` is supplied (the chunk FTS hits already
    computed by ``_hybrid_fusion``), reuse it instead of re-querying the
    chunk index, avoiding a redundant FTS pass over ``memory_chunks_fts``.
    """
    try:
        if merged_chunks is None:
            chunk_hits = _search_chunks_enhanced(db, fts_query, limit=limit * 2)
            if not chunk_hits:
                return results
            merged_chunks = _merge_chunk_hits(chunk_hits)
        if not merged_chunks:
            return results
        seen_ids = {r[0] for r in results}
        chunk_parent_ids = [p_id for p_id, _, _, _, _ in merged_chunks if p_id not in seen_ids]
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
                    for p_id, _, _, _, _ in merged_chunks
                    if p_id not in seen_ids and p_id in chunk_rows
                ]
                if check_ids:
                    placeholders = ",".join("?" * len(check_ids))
                    _rows = db.execute(
                        f"SELECT id, valid_to FROM tenant_memories WHERE id IN ({placeholders})",
                        check_ids,
                    ).fetchall()
                    invalid_ids = {row[0] for row in _rows if row[1] not in (None, "")}
        for parent_id, chunk_idx, chunk_text, chunk_rank, chunk_count in merged_chunks:
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
