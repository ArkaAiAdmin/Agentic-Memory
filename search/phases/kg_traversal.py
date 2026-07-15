"""Phase 10 KG concept boost and multi-hop KG traversal."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

from infra.error_counter import increment as _phase_inc
from search.phases._db_utils import _fetch_rows_by_ids

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

# Stop words for content-based entity extraction — common words that
# aren't useful as KG entity names.
_STOP_WORDS = frozenset({
    "this", "that", "with", "from", "have", "been", "were", "they",
    "their", "about", "would", "could", "should", "will", "just",
    "also", "into", "more", "than", "when", "what", "which", "where",
    "some", "only", "other", "each", "very", "most", "such", "then",
    "them", "these", "those", "does", "done", "make", "like", "over",
    "tell", "back", "been", "many", "much", "well", "your", "they",
    "file", "data", "user", "type", "name", "line", "code", "test",
    "func", "true", "false", "none", "self", "init", "main", "http",
    "com", "org", "net", "www", "path", "link", "edit", "view",
})

# Gap (in rank units) below the best genuine result where KG-discovered
# items are placed. They are strictly supplementary: a weak direct match
# is never displaced by an arbitrary synthetic rank, and within the
# supplementary block stronger edges rank higher.
_KG_SUPPLEMENT_GAP = 0.5


def _entity_name_to_memory_id(
    db: AnyConnection, entity_name: str, seen_ids: set[str]
) -> list[str]:
    """Map a KG entity name to memory IDs whose slug or ID contains it.

    Tries three patterns in order:
      1. Exact slug match (``%/{entity_name}``)
      2. Substring match (``%{entity_name}%``)
      3. Hyphenated slug component match (``%-{entity_name}`` or ``%{entity_name}-%``)

    Returns up to 3 memory IDs not already in ``seen_ids``.
    """
    patterns = [
        f"%/{entity_name}",
        f"%{entity_name}%",
        f"%-{entity_name}",
        f"%{entity_name}-%",
    ]
    found: list[str] = []
    try:
        placeholders = ' OR '.join('id LIKE ?' for _ in patterns)
        rows = db.execute(
            f"SELECT id FROM tenant_memories WHERE ({placeholders}) AND deleted_at IS NULL LIMIT 3",
            patterns,
        ).fetchall()
        for row in rows:
            mid = row[0] if isinstance(row, sqlite3.Row) else row[0]
            if mid not in seen_ids and mid not in found:
                found.append(mid)
                if len(found) >= 3:
                    return found
    except sqlite3.Error:
        pass
    return found


def _phase_nine_kg_boost(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
    repo_filter: str = "",
    category: str | None = None,
) -> list:
    """Phase 10: KG concept/centrality boost — expand candidates via KG edges.

    Extracts entity tokens from current result memory IDs (the slug after
    the category prefix), looks up matching ``kg_entities``, traverses
    1-hop edges, and adds memory IDs corresponding to the related entities
    to the candidate set.  No-op when the KG is disabled, empty, or when
    all related entities are already in the result set.

    Only fires when the candidate set is non-empty (no reason to traverse
    the KG from nothing), and caps the number of new candidates to
    ``limit`` so the boost doesn't dominate reranking.
    """
    if not results:
        return results
    try:
        from knowledge_graph import KG_ENABLED

        if not KG_ENABLED:
            return results
    except (ImportError, AttributeError):
        return results

    try:
        seen_ids = {r[0] for r in results}
        # Extract entity tokens from result memory IDs AND content.
        entity_tokens: set[str] = set()
        for r in results:
            mid = r[0]
            # Slug-based extraction (legacy)
            if "/" in mid:
                slug = mid.split("/", 1)[1]
                entity_tokens.add(slug.lower())
                for word in re.findall(r"[a-z0-9]+", slug.lower()):
                    if len(word) > 2:
                        entity_tokens.add(word)
            # Content-based extraction: pull tokens from the content field
            # (r[1] is content in the result tuple).  This catches entities
            # that aren't reflected in the slug.
            content = r[1] if len(r) > 1 and isinstance(r[1], str) else ""
            if content:
                for word in re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", content[:3000]):
                    if len(word) > 3:
                        entity_tokens.add(word.lower())
                for word in re.findall(r"[a-z0-9]{4,}", content[:3000].lower()):
                    if word not in _STOP_WORDS and len(word) > 3:
                        entity_tokens.add(word)

        if not entity_tokens:
            return results

        # Look up matching KG entities (WARN-2: batched IN instead of per-token round trips).
        kg_entity_ids: set[int] = set()
        token_sample = list(entity_tokens)[:20]
        if token_sample:
            kg_ph = ",".join("?" * len(token_sample))
            try:
                rows = db.execute(
                    f"SELECT id FROM kg_entities WHERE name IN ({kg_ph})",
                    token_sample,
                ).fetchall()
                for row in rows:
                    kg_entity_ids.add(row[0] if not isinstance(row, sqlite3.Row) else row[0])
            except sqlite3.Error:
                pass

        if not kg_entity_ids:
            return results

        # Traverse 1-hop edges to find related entities.
        eid_list = list(kg_entity_ids)
        placeholders = ",".join("?" * len(eid_list))
        # Build NOT IN params: we exclude entities already matched to avoid
        # finding the same entities we started from.
        not_in_placeholders = ",".join("?" * len(eid_list))
        related_rows = db.execute(
            f"SELECT DISTINCT e.id, e.name, ed.weight "
            f"FROM kg_edges ed "
            f"JOIN kg_entities e ON (e.id = CASE WHEN ed.source_id IN ({placeholders}) THEN ed.target_id ELSE ed.source_id END) "
            f"WHERE (ed.source_id IN ({not_in_placeholders}) OR ed.target_id IN ({not_in_placeholders})) "
            f"AND e.id NOT IN ({not_in_placeholders}) "
            f"AND ed.invalid_at IS NULL "
            f"ORDER BY ed.weight DESC "
            f"LIMIT ?",
            eid_list + eid_list + eid_list + eid_list + [limit * 2],
        ).fetchall()

        if not related_rows:
            return results

        # Map related entity names to memory IDs, carrying the max edge
        # weight seen for each memory.  The weight (not an arbitrary
        # synthetic rank) drives how KG-discovered items are ordered.
        new_memory_ids: list[str] = []
        new_memory_weights: dict[str, float] = {}
        for row in related_rows:
            if isinstance(row, sqlite3.Row):
                entity_name = row["name"]
                edge_weight = row["weight"]
            else:
                entity_name = row[1]
                edge_weight = row[2]
            edge_weight = float(edge_weight) if edge_weight is not None else 1.0
            matches = _entity_name_to_memory_id(db, entity_name, seen_ids)
            for mid in matches:
                if mid not in new_memory_ids:
                    new_memory_ids.append(mid)
                prev = new_memory_weights.get(mid)
                if prev is None or edge_weight > prev:
                    new_memory_weights[mid] = edge_weight
                if len(new_memory_ids) >= limit:
                    break
            if len(new_memory_ids) >= limit:
                break

        if not new_memory_ids:
            return results

        # Fetch full rows and append them.  KG-discovered items are placed
        # *below* the genuine result set (supplementary) and ordered by edge
        # weight, so a weak direct match is never displaced by an arbitrary
        # synthetic rank.  base_rank is the best (smallest) genuine rank.
        new_rows = _fetch_rows_by_ids(
            db, new_memory_ids,
            extra_filter=repo_filter,
            extra_params=(category,) if category else (),
        )
        base_rank = min((float(r[5]) for r in results if len(r) > 5), default=0.0)
        added = 0
        for mid in new_memory_ids:
            row = new_rows.get(mid)
            if row is not None and mid not in seen_ids:
                # Build a 12-element tuple matching the canonical results format:
                # (id, content, source_file, tags, created_at, rank, fitness,
                #  importance, pinned, last_accessed, metadata, access_count)
                w = new_memory_weights.get(mid, 1.0)
                w_norm = min(1.0, max(0.0, w))
                rank_val = base_rank + _KG_SUPPLEMENT_GAP * (1.0 - w_norm)
                results.append((
                    mid,
                    row[1] if len(row) > 1 else "",
                    row[2] if len(row) > 2 else "",
                    row[3] if len(row) > 3 else None,
                    row[4] if len(row) > 4 else "",
                    rank_val,
                    row[5] if len(row) > 5 else None,
                    row[6] if len(row) > 6 else None,
                    row[7] if len(row) > 7 else None,
                    row[8] if len(row) > 8 else None,
                    row[9] if len(row) > 9 else None,
                    row[10] if len(row) > 10 else 1,
                ))
                seen_ids.add(mid)
                added += 1
                if added >= limit:
                    break
        return results
    except Exception as e:
        _phase_inc("search.kg_boost", e)
        logger.warning("_phase_nine_kg_boost failed: %s", e)
        return results


def _phase_ten_multi_hop_kg(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
    repo_filter: str = "",
    category: str | None = None,
) -> list:
    """Phase 10: Multi-hop KG traversal for cross-category queries.

    Three-round traversal from query entities:
      * Round 1: Extract entity-like tokens from the query, find matching
        ``kg_entities``.
      * Round 2: Traverse 1-hop edges from those entities to find
        intermediate entities.
      * Round 3: Traverse another hop to reach result entities.

    Each discovered memory is scored by shortest path length:
      * 1-hop (direct query match) → ``0.9``
      * 2-hop (one intermediate entity) → ``0.7``
      * 3-hop (two intermediates) → ``0.55``

    Scores decay by an additional ``×0.85`` for entities with edge weight
    below 0.5.  Results already in the candidate set are re-scored with
    the higher of their existing and multi-hop score.

    Only fires when the KG is enabled and the query has enough tokens
    to extract entities from.
    """
    try:
        from knowledge_graph import KG_ENABLED

        if not KG_ENABLED:
            return results
    except (ImportError, AttributeError):
        return results

    # Only run for queries with at least 3 meaningful tokens.
    query_tokens = [t.lower() for t in re.findall(r"[a-z0-9]{3,}", query.lower())]
    if len(query_tokens) < 2:
        return results

    try:
        seen_ids = {r[0] for r in results}
        # Round 1: find KG entities matching query tokens.
        # Round 1: find KG entities matching query tokens (WARN-2: batched IN).
        query_entity_ids: set[int] = set()
        entity_name_to_id: dict[str, int] = {}
        token_sample = query_tokens[:10]
        if token_sample:
            mh_ph = ",".join("?" * len(token_sample))
            try:
                rows = db.execute(
                    f"SELECT id, name FROM kg_entities WHERE name IN ({mh_ph})",
                    token_sample,
                ).fetchall()
                for row in rows:
                    eid = row[0] if not isinstance(row, sqlite3.Row) else row[0]
                    ename = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
                    query_entity_ids.add(eid)
                    entity_name_to_id[ename] = eid
            except sqlite3.Error:
                pass

        if not query_entity_ids:
            return results

        # Round 2: traverse 1-hop edges from query entities → intermediate.
        hop1_entities: dict[int, float] = {}
        eid_list = list(query_entity_ids)
        placeholders = ",".join("?" * len(eid_list))
        hop1_params = tuple(eid_list) * 3 + (limit * 5,)
        hop1_rows = db.execute(
            f"SELECT DISTINCT "
            f"  CASE WHEN ed.source_id IN ({placeholders}) THEN ed.target_id ELSE ed.source_id END AS neighbor_id, "
            f"  ed.weight, "
            f"  1 AS hop_distance "
            f"FROM kg_edges ed "
            f"WHERE (ed.source_id IN ({placeholders}) OR ed.target_id IN ({placeholders})) "
            f"AND ed.invalid_at IS NULL "
            f"LIMIT ?",
            hop1_params,
        ).fetchall()
        for row in hop1_rows:
            nid = row[0] if not isinstance(row, sqlite3.Row) else row["neighbor_id"]
            weight = row[1] if not isinstance(row, sqlite3.Row) else row["weight"]
            if nid not in query_entity_ids:
                weight_float = float(weight) if weight is not None else 1.0
                existing = hop1_entities.get(nid)
                if existing is None or existing < weight_float:
                    hop1_entities[nid] = weight_float

        # Round 3: traverse 2-hop edges → result entities.
        hop2_entities: dict[int, float] = {}
        if hop1_entities:
            hop1_ids = list(hop1_entities.keys())
            hp1 = ",".join("?" * len(hop1_ids))
            not_in_ids = list(query_entity_ids) + hop1_ids
            not_in_ph = ",".join("?" * len(not_in_ids))
            hop2_params = tuple(hop1_ids) * 4 + tuple(not_in_ids) + (limit * 3,)
            hop2_rows = db.execute(
                f"SELECT DISTINCT "
                f"  CASE WHEN ed.source_id IN ({hp1}) THEN ed.target_id ELSE ed.source_id END AS result_id, "
                f"  ed.weight, "
                f"  2 AS hop_distance "
                f"FROM kg_edges ed "
                f"WHERE (ed.source_id IN ({hp1}) OR ed.target_id IN ({hp1})) "
                f"AND ed.invalid_at IS NULL "
                f"AND CASE WHEN ed.source_id IN ({hp1}) THEN ed.target_id ELSE ed.source_id END "
                f"  NOT IN ({not_in_ph}) "
                f"LIMIT ?",
                hop2_params,
            ).fetchall()
            for row in hop2_rows:
                rid = row[0] if not isinstance(row, sqlite3.Row) else row["result_id"]
                weight = row[1] if not isinstance(row, sqlite3.Row) else row["weight"]
                weight_float = float(weight) if weight is not None else 1.0
                existing = hop2_entities.get(rid)
                if existing is None or existing < weight_float:
                    hop2_entities[rid] = weight_float

        # Collect all result entity IDs with their hop paths.
        all_result_entities: dict[int, tuple[float, int]] = {}
        # 1-hop: entities directly connected to query entities.
        for eid, weight in hop1_entities.items():
            all_result_entities[eid] = (0.9, 1)
        # 2-hop: entities two steps away.
        for eid, weight in hop2_entities.items():
            existing_score = all_result_entities.get(eid)
            score = 0.7
            if weight < 0.5:
                score *= 0.85
            if existing_score is None or score > existing_score[0]:
                all_result_entities[eid] = (score, 2)

        if not all_result_entities:
            return results

        # Fetch entity names.
        result_eid_list = list(all_result_entities.keys())
        re_ph = ",".join("?" * len(result_eid_list))
        entity_names: dict[int, str] = {}
        try:
            name_rows = db.execute(
                f"SELECT id, name FROM kg_entities WHERE id IN ({re_ph})",
                result_eid_list,
            ).fetchall()
            for row in name_rows:
                eid = row[0] if not isinstance(row, sqlite3.Row) else row[0]
                ename = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
                entity_names[eid] = ename
        except sqlite3.Error:
            pass

        # Map entity names to memory IDs.
        new_memory_scores: list[tuple[str, float]] = []
        for eid, (score, hop_count) in all_result_entities.items():
            ename = entity_names.get(eid, "")
            if not ename:
                continue
            matches = _entity_name_to_memory_id(db, ename, set())
            for mid in matches:
                new_memory_scores.append((mid, score))

        if not new_memory_scores:
            return results

        # Merge into results.  Multi-hop discoveries are placed strictly
        # below the genuine result set (and below phase-9 KG boosts),
        # ordered by their hop/edge score — never via an arbitrary synthetic
        # rank, so a weak direct match is never displaced.
        new_rows = _fetch_rows_by_ids(
            db, [m[0] for m in new_memory_scores],
            extra_filter=repo_filter,
            extra_params=(category,) if category else (),
        )
        base_rank = min((float(r[5]) for r in results if len(r) > 5), default=0.0)
        for mid, score in new_memory_scores:
            row = new_rows.get(mid)
            if row is None:
                continue
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            s_norm = min(1.0, max(0.0, float(score)))
            rank_val = base_rank + _KG_SUPPLEMENT_GAP * (1.0 - s_norm)
            results.append((
                mid,
                row[1] if len(row) > 1 else "",
                row[2] if len(row) > 2 else "",
                row[3] if len(row) > 3 else None,
                row[4] if len(row) > 4 else "",
                rank_val,
                row[5] if len(row) > 5 else None,
                row[6] if len(row) > 6 else None,
                row[7] if len(row) > 7 else None,
                row[8] if len(row) > 8 else None,
                row[9] if len(row) > 9 else None,
                row[10] if len(row) > 10 else 1,
            ))

        return results
    except Exception as e:
        _phase_inc("search.multi_hop_kg", e)
        logger.warning("_phase_ten_multi_hop_kg failed: %s", e)
        return results
