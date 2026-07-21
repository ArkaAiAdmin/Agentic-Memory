"""Typed, validated view of the search-relevant subset of ``MemoryConfig``.

Historically the search pipeline performed 8+ independent ``get_config()``
lookups — one per ``_get_*`` getter — each with its own ad-hoc fallback and
JSON parse.  ``SearchConfig`` collapses that into a single pydantic model
built once per process via :func:`get_search_config`.

Every default mirrors the prior ``_get_*`` getter fallbacks exactly, so a
default config produces identical behaviour.  Values are sourced from the
live ``MemoryConfig`` singleton; if a field is missing or unreadable the
prior fallback is used.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Mirrors the prior ``_RERANK_WEIGHTS`` module constant in search/scoring.py.
_DEFAULT_RERANK_WEIGHTS: dict[str, float] = {
    "bm25": 0.40,
    "fitness": 0.20,
    "importance": 0.15,
    "pinned": 0.10,
    "recency": 0.10,
    "tag_match": 0.05,
}


class SearchConfig(BaseModel):
    """Search-relevant config, validated and typed.

    Field defaults are the canonical fallbacks used before this model
    existed.  Do not change a default without also updating the matching
    ``_get_*`` getter it replaced (and its test).
    """

    rerank_weights: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_RERANK_WEIGHTS)
    )
    strong_bm25_threshold: float = 0.95
    rerank_half_life_days: float = 180.0
    cross_encoder_blend: float = 0.6
    late_interaction_blend: float = 0.3
    ce_blend: float = 0.85
    ce_chunk_blend: float = 0.7
    embedding_score_threshold: float = 0.25
    embedding_prefilter_enabled: bool = True
    embedding_prefilter_k: int = 200
    temporal_decay_weight: float = 0.15
    entity_boost_factor: float = 1.15
    inference_embedding_downweight: float = 0.3
    temporal_compare_boost: float = 1.1


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_search_config() -> SearchConfig:
    """Construct a ``SearchConfig`` from the live ``MemoryConfig``.

    Preserves the exact fallback semantics of the legacy ``_get_*`` getters:
    a missing/unreadable field falls back to its canonical default rather
    than raising.
    """
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("get_config() unavailable; using SearchConfig defaults: %s", e)
        return SearchConfig()

    # rerank_weights: empty/None -> default dict (mirrors _get_rerank_weights).
    rerank_weights = _DEFAULT_RERANK_WEIGHTS
    try:
        raw = getattr(cfg, "rerank_weights", None)
        if raw:
            parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(parsed, dict):
                rerank_weights = {k: float(v) for k, v in parsed.items()}
    except Exception:
        rerank_weights = _DEFAULT_RERANK_WEIGHTS

    # rerank_half_life_days: legacy nested path rerank.half_life_days fallback.
    rerank_half_life_days = 180.0
    try:
        rerank_half_life_days = float(getattr(cfg, "rerank_half_life_days", 180.0))
    except Exception:
        rerank = getattr(cfg, "rerank", None)
        if rerank is not None:
            try:
                rerank_half_life_days = float(getattr(rerank, "half_life_days", 180.0))
            except Exception:
                rerank_half_life_days = 180.0

    return SearchConfig(
        rerank_weights=rerank_weights,
        strong_bm25_threshold=_coerce_float(
            getattr(cfg, "strong_bm25_threshold", 0.95), 0.95
        ),
        rerank_half_life_days=rerank_half_life_days,
        cross_encoder_blend=_coerce_float(
            getattr(cfg, "cross_encoder_blend", 0.6), 0.6
        ),
        late_interaction_blend=_coerce_float(
            getattr(cfg, "late_interaction_blend", 0.3), 0.3
        ),
        ce_blend=_coerce_float(getattr(cfg, "ce_blend", 0.85), 0.85),
        ce_chunk_blend=_coerce_float(getattr(cfg, "ce_chunk_blend", 0.7), 0.7),
        embedding_score_threshold=_coerce_float(
            getattr(cfg, "embedding_score_threshold", 0.25), 0.25
        ),
        embedding_prefilter_enabled=bool(getattr(cfg, "embedding_prefilter_enabled", True)),
        embedding_prefilter_k=int(getattr(cfg, "embedding_prefilter_k", 200)),
        temporal_decay_weight=_coerce_float(
            getattr(cfg, "temporal_decay_weight", 0.15), 0.15
        ),
        entity_boost_factor=_coerce_float(
            getattr(cfg, "entity_boost_factor", 1.15), 1.15
        ),
        inference_embedding_downweight=_coerce_float(
            getattr(cfg, "inference_embedding_downweight", 0.3), 0.3
        ),
    )


_cache: Optional[SearchConfig] = None
_cache_cfg_id: Optional[int] = None
_lock = threading.Lock()


def get_search_config() -> SearchConfig:
    """Return the process-wide ``SearchConfig``.

    Rebuilds only when the underlying ``MemoryConfig`` singleton changes
    (its ``id``), which covers test re-patching and any future hot-reload.
    In production ``get_config()`` is a stable singleton, so this returns a
    cached instance after the first call.
    """
    global _cache, _cache_cfg_id
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        cfg_id = id(cfg)
    except Exception:
        cfg = None
        cfg_id = None
    if _cache is None or cfg_id != _cache_cfg_id:
        with _lock:
            if _cache is None or cfg_id != _cache_cfg_id:
                _cache = _build_search_config()
                _cache_cfg_id = cfg_id
    return _cache


def reload_search_config() -> SearchConfig:
    """Force a rebuild of the cached ``SearchConfig``.

    Call this from the config hot-reload path so the cached view tracks a
    reloaded ``MemoryConfig``.
    """
    global _cache, _cache_cfg_id
    with _lock:
        _cache = _build_search_config()
        try:
            from infra._lazy_imports import get_config

            _cache_cfg_id = id(get_config())
        except Exception:
            _cache_cfg_id = None
    return _cache
