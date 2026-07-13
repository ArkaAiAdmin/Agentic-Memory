"""Post-rank enrichment for the search pipeline (Phase 9b -- Rank-First Lock).

PR1.1 contract
---------------
After the Cross-Encoder (CE) reranking stage owns the final *order* of
results, NO later code may change the relative order of those results. The
four historical "enrichment" passes -- concept boost, centrality boost,
Jaccard-surprise penalty, and temporal decay -- used to *mutate* each
result's ``final_score`` (the ranking key). That is forbidden under the
rank-first lock.

This module is the ONLY place post-CE enrichment runs.
``_apply_post_rank_metadata`` consumes an ordered list of result items
(dicts) and returns a NEW list with the SAME relative order, attaching
purely informational envelope fields (``concept_boost``,
``centrality_boost``, ``jaccard_surprise``, ``temporal_decay``). It
never re-sorts, never drops items, and never mutates the ranking
``final_score``.

The numeric value stored for each envelope key is exactly the multiplicative
factor the corresponding legacy mutator would have applied to
``final_score`` -- so a consumer can still reconstruct the legacy display
value as ``final_score * factor`` *without that factor ever influencing
ranking*.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# -- Mirror constants from search.scoring (kept identical on purpose) -------
_CONCEPT_BOOST = 1.35
_CONCEPT_MEMBERSHIP_BOOST = 1.20
_MAX_BOOST = 1.50
_CENTRALITY_BOOST_FACTOR = 1.25
_CENTRALITY_BOOST_MAX = 1.25


def _apply_post_rank_metadata(
    items: list,
    query: str,
    db_path: Any,
    as_of: Optional[float] = None,
) -> list:
    """Attach enrichment metadata to each result item WITHOUT changing order.

    Returns a NEW list with the SAME relative order as ``items``. Only adds
    keys (``concept_boost``, ``centrality_boost``, ``jaccard_surprise``,
    ``temporal_decay``) to each item dict; never re-sorts, never drops
    items, never mutates ``item["final_score"]`` (the ranking key).

    The function iterates ``items`` strictly in order, copying each dict and
    appending the copy. There is no code path that reorders or filters, so
    order preservation is structural and provable.
    """
    if not items:
        return list(items)
    try:
        concept_map = _load_concept_map(db_path)
        centrality_map = _load_centrality_map(db_path)
        jaccard_map = _load_jaccard_map(items, query)
        temporal_priors = _load_temporal_priors(db_path)
    except Exception as exc:  # pragma: no cover - best-effort envelope
        logger.warning("Enrichment metadata degraded to neutral (best-effort): %s", exc)
        concept_map, centrality_map, jaccard_map, temporal_priors = {}, {}, {}, {}

    out = []
    for item in items:
        if not isinstance(item, dict):
            # Non-dict items pass through untouched; order preserved.
            out.append(item)
            continue
        new_item = dict(item)  # shallow copy: the original dict is never mutated
        new_item["concept_boost"] = _concept_factor(new_item, query, concept_map)
        new_item["centrality_boost"] = _centrality_factor(new_item, centrality_map)
        new_item["jaccard_surprise"] = _jaccard_factor(item, query, jaccard_map)
        new_item["temporal_decay"] = _temporal_factor(item, as_of, temporal_priors)
        out.append(new_item)
    return out


# -- Table loaders (monkeypatchable in tests) -----------------------------


def _load_concept_map(db_path: Any) -> dict[str, set[int]]:
    """Return ``{concept_note_id: set(entity_id)}`` for concept notes."""
    from infra._lazy_imports import connection_pool

    db = connection_pool.get(str(db_path), timeout=5.0)
    try:
        rows = db.execute(
            "SELECT id, metadata FROM memories WHERE category = 'concepts' AND deleted_at IS NULL"
        ).fetchall()
    finally:
        try:
            connection_pool.put(db)
        except Exception:
            pass
    concept_entities: dict[str, set[int]] = {}
    for note_id, meta_json in rows:
        if not meta_json:
            continue
        try:
            meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
            concept_entities[note_id] = {
                int(e) for e in (meta.get("entities") or []) if e is not None
            }
        except Exception:
            continue
    return concept_entities


def _load_centrality_map(db_path: Any) -> dict[int, float]:
    """Return ``{entity_id: betweenness}`` for entities with centrality."""
    from infra._lazy_imports import connection_pool

    db = connection_pool.get(str(db_path), timeout=5.0)
    try:
        rows = db.execute(
            "SELECT id, betweenness FROM kg_entities WHERE betweenness IS NOT NULL"
        ).fetchall()
    finally:
        try:
            connection_pool.put(db)
        except Exception:
            pass
    return {int(eid): float(score) for eid, score in rows}


def _load_jaccard_map(items: list, query: str) -> dict:
    """Return ``{note_id: last_access_query}`` for surprise scoring."""
    from adaptive_retention import get_last_access_queries_batch

    if not query:
        return {}
    ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
    if not ids:
        return {}
    res: dict[str, Any] = get_last_access_queries_batch(ids)
    return res


# -- Per-item factor computations (mirror search.scoring exactly) ------------


def _concept_factor(item: dict, query: str, concept_map: dict[str, set[int]]) -> float:
    if not concept_map or not query:
        return 1.0
    q_tokens = {
        t.lower() for t in re.findall(r"\b[a-z][a-z\-]+\b", query.lower()) if len(t) >= 3
    }
    note_id = item.get("id")
    source_file = item.get("source_file")
    try:
        meta = item.get("metadata") or {}
        result_entities = {
            int(e) for e in (meta.get("entities") or []) if e is not None
        }
    except Exception:
        result_entities = set()
    boosted = 1.0
    for cid in sorted(concept_map):
        centities = concept_map[cid]
        if note_id == cid or cid in (source_file or ""):
            boosted = max(boosted, _CONCEPT_BOOST)
        elif result_entities and centities and (result_entities & centities):
            boosted = max(boosted, _CONCEPT_MEMBERSHIP_BOOST)
    if q_tokens:
        cname_tokens = set()
        for cid in concept_map:
            slug = cid.split("/")[-1] if "/" in cid else cid
            cname_tokens.update(slug.replace("-", " ").lower().split())
        if cname_tokens and (q_tokens & cname_tokens):
            boosted *= 1.10
    # Cap mirrors _apply_concept_boost: the final boosted score is
    # min(boosted, _MAX_BOOST * final_score), so for a positive score the
    # relative factor applied is min(boosted, _MAX_BOOST).
    return min(boosted, _MAX_BOOST)


def _centrality_factor(item: dict, centrality_map: dict[int, float]) -> float:
    if not centrality_map:
        return 1.0
    try:
        from infra._lazy_imports import get_config

        if str(get_config().graph_centrality_boost).lower() not in ("1", "true", "yes"):
            return 1.0
    except Exception:
        return 1.0
    meta = item.get("metadata") or {}
    try:
        result_entities = [int(e) for e in (meta.get("entities") or []) if e is not None]
    except Exception:
        result_entities = []
    if not result_entities:
        return 1.0
    entity_centralities = [centrality_map[e] for e in result_entities if e in centrality_map]
    if not entity_centralities:
        return 1.0
    max_centrality = max(centrality_map.values())
    avg = sum(entity_centralities) / len(entity_centralities)
    normalized = avg / max(max_centrality, 1e-9)
    boost = 1.0 + normalized * (_CENTRALITY_BOOST_FACTOR - 1.0)
    return min(boost, _CENTRALITY_BOOST_MAX)


def _jaccard_factor(item: dict, query: str, jaccard_map: dict) -> float:
    if not query or not jaccard_map:
        return 1.0
    note_id = item.get("id")
    last_q = jaccard_map.get(note_id) if note_id else None
    if not last_q:
        return 1.0
    try:
        q_tokens = set(re.findall(r"\w+", query.lower()))
        last_tokens = set(re.findall(r"\w+", last_q.lower()))
    except Exception:
        return 1.0
    if not q_tokens or not last_tokens:
        return 1.0
    inter = len(q_tokens & last_tokens)
    union = len(q_tokens | last_tokens)
    jaccard = inter / union if union > 0 else 0.0
    surprise = 1.0 - jaccard
    return 1.0 - 0.1 * surprise


def _temporal_factor(
    item: dict,
    as_of: Optional[float],
    temporal_priors: Optional[dict[str, float]] = None,
) -> float:
    from search.scoring import _sp_lazy, _temporal_decay_factor

    decay_weight = 0.15
    try:
        from infra._lazy_imports import get_config

        decay_weight = float(get_config().temporal_decay_weight)
    except Exception:
        pass
    if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "off" or decay_weight <= 0:
        return 1.0
    created = item.get("created") or ""
    last_accessed = item.get("last_accessed")
    category = item.get("category")

    # Resolve half life: per-category DB prior -> per-category default -> global default
    half_life = None
    if category:
        if temporal_priors and category in temporal_priors:
            half_life = temporal_priors[category]
        elif category in DEFAULT_TEMPORAL_PRIORS:
            half_life = DEFAULT_TEMPORAL_PRIORS[category]

    now_ts = time.time() if as_of is None else as_of
    decay = _temporal_decay_factor(created, now_ts, last_accessed, as_of, half_life=half_life)
    return 1.0 - decay_weight + decay_weight * decay


DEFAULT_TEMPORAL_PRIORS = {
    "lessons": 180.0,
    "concepts": 730.0,
    "sessions": 14.0,
    "preferences": 90.0,
    "projects": 365.0,
    "decisions": 365.0,
    "facts": 90.0,
}


def _load_temporal_priors(db_path: Any) -> dict[str, float]:
    """Return ``{category: half_life_days}`` from memory_temporal_priors."""
    from infra._lazy_imports import connection_pool

    db = connection_pool.get(str(db_path), timeout=5.0)
    try:
        rows = db.execute(
            "SELECT category, half_life_days FROM memory_temporal_priors"
        ).fetchall()
        return {str(cat): float(days) for cat, days in rows}
    except Exception:
        return {}
    finally:
        try:
            connection_pool.put(db)
        except Exception:
            pass
