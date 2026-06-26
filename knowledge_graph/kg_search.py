from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time as _time
from collections import OrderedDict as _OrderedDict
from pathlib import Path
from typing import Any, Optional, cast

from .kg_schema import ensure_kg_schema
from .kg_db import index_kg_for_memory

logger = logging.getLogger(__name__)


# LRU+TTL cache for graph_search results. Reduces repeated FTS5/LIKE
# roundtrips in agentic loops where the same query is issued many times
# in quick succession. Entries are keyed by (query, limit, max_hops, as_of)
# and invalidated by TTL (default 60s) so DB updates are eventually
# reflected. Set _GRAPH_CACHE_TTL_S=0 to disable.
def _get_graph_cache_max() -> int:
    try:
        from _lazy_imports import get_config

        return int(get_config().graph_cache_max)
    except Exception:
        return 50


def _get_graph_cache_ttl_s() -> float:
    try:
        from _lazy_imports import get_config

        return float(get_config().graph_cache_ttl_s)
    except Exception:
        return 60.0


_GRAPH_CACHE_MAX = _get_graph_cache_max()
_GRAPH_CACHE_TTL_S = _get_graph_cache_ttl_s()
_graph_cache: _OrderedDict = _OrderedDict()
_graph_cache_lock = threading.Lock()


def _graph_cache_get(key: tuple) -> dict | None:
    """Return cached graph result if fresh, else None. Updates LRU order."""
    now = _time.monotonic()
    with _graph_cache_lock:
        entry = _graph_cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if _get_graph_cache_ttl_s() > 0 and (now - ts) > _get_graph_cache_ttl_s():
            _graph_cache.pop(key, None)
            return None
        _graph_cache.move_to_end(key)
        return cast(dict[Any, Any] | None, value)


def _graph_cache_put(key: tuple, value: dict) -> None:
    """Store a graph result in the LRU cache, evicting oldest if at capacity."""
    if _get_graph_cache_ttl_s() <= 0:
        return
    with _graph_cache_lock:
        _graph_cache[key] = (_time.monotonic(), value)
        _graph_cache.move_to_end(key)
        while len(_graph_cache) > _get_graph_cache_max():
            _graph_cache.popitem(last=False)


def clear_graph_cache() -> None:
    """Clear the graph_search cache. Useful for tests."""
    with _graph_cache_lock:
        _graph_cache.clear()


def _match_query_entities(conn, query_lower: str, limit: int) -> list:
    """Find matching entities in the knowledge graph.

    Tries FTS5 first (with OR-based matching for multi-word queries);
    falls back through exact, prefix, then substring LIKE matches if
    FTS5 returns nothing. Returns up to ``limit * 3`` raw rows of
    (id, name, entity_type, mentions). Extracted 2026-06-22.
    """
    entities: list = []
    try:
        tokens = query_lower.split()
        if len(tokens) == 1:
            fts_query = f'"{tokens[0]}"'
        else:
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
        entities = conn.execute(
            "SELECT ge.id, ge.name, ge.entity_type, ge.mentions "
            "FROM kg_entities_fts "
            "JOIN kg_entities ge ON ge.rowid = kg_entities_fts.rowid "
            "WHERE kg_entities_fts MATCH ? "
            "LIMIT ?",
            (fts_query, limit * 3),
        ).fetchall()
    except Exception:
        logger.warning("FTS5 query failed for entity search, falling back to LIKE scan")

    if not entities:
        entities = conn.execute(
            "SELECT id, name, entity_type, mentions FROM kg_entities "
            "WHERE name = ? LIMIT ?",
            (query_lower, limit * 3),
        ).fetchall()
    if not entities:
        entities = conn.execute(
            "SELECT id, name, entity_type, mentions FROM kg_entities "
            "WHERE name LIKE ? LIMIT ?",
            (f"{query_lower}%", limit * 3),
        ).fetchall()
    if not entities:
        entities = conn.execute(
            "SELECT id, name, entity_type, mentions FROM kg_entities "
            "WHERE name LIKE ? LIMIT ?",
            (f"%{query_lower}%", limit * 3),
        ).fetchall()
    return entities


def _temporal_edge_clause(as_of: str | None) -> tuple[str, list]:
    """Build the temporal filter SQL fragment + params for kg_edges
    queries. Returns (" AND ...", params) so callers can splat the
    clause into a larger query string.

    Extracted 2026-06-22 from graph_search() — the same clause was
    inlined three times.
    """
    if as_of is not None:
        return (
            " AND e.valid_at <= ? AND (e.invalid_at IS NULL OR e.invalid_at >= ?)",
            [as_of, as_of],
        )
    return " AND e.invalid_at IS NULL", []


def _row_to_edge_dict(edge) -> dict:
    """Convert a kg_edges SELECT row into the response dict shape.

    Extracted 2026-06-22 — used in both 1-hop and 2-hop edge assembly.
    """
    return {
        "id": edge[0],
        "source": edge[1],
        "source_type": edge[2],
        "relation": edge[3],
        "target": edge[4],
        "target_type": edge[5],
        "weight": edge[6],
    }


def _row_to_entity_dict(e) -> dict:
    """Convert a kg_entities SELECT row into the response dict shape.

    Extracted 2026-06-22.
    """
    return {
        "id": e[0],
        "name": e[1],
        "entity_type": e[2],
        "mentions": e[3],
    }


def _assemble_1hop(
    conn, entities: list, entity_ids: list, limit: int, as_of: str | None
) -> tuple[list, list, set, dict]:
    """Fetch 1-hop edges for the matched entities. Returns
    ``(edge_list, endpoint_entities, hop1_ids, name_to_id)``.

    The endpoint_entities list contains the rows for entities that
    were referenced as edge endpoints but weren't in the original
    matched-entities set. The caller merges these into entity_map so
    callers see 1-hop neighbors in the response.

    Extracted 2026-06-22 from graph_search().  H5 fix: batch endpoint
    entity lookups instead of N+1 queries.
    """
    edge_list: list = []
    endpoint_entities: list = []
    name_to_id: dict = {e[1]: e[0] for e in entities}
    hop1_ids: set = set()
    if not entity_ids:
        return edge_list, endpoint_entities, hop1_ids, name_to_id

    placeholders = ",".join("?" * len(entity_ids))
    temporal_clause, temporal_params = _temporal_edge_clause(as_of)
    edges = conn.execute(
        f"""SELECT e.id, s.name, s.entity_type, e.relation,
                   t.name, t.entity_type, e.weight
           FROM kg_edges e
           JOIN kg_entities s ON e.source_id = s.id
           JOIN kg_entities t ON e.target_id = t.id
           WHERE (e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders}))
           {temporal_clause}
           ORDER BY e.weight DESC
           LIMIT ?""",
        entity_ids + entity_ids + temporal_params + [limit * 3],
    ).fetchall()

    # H5 fix: collect all endpoint names first, then batch lookup
    endpoint_names: set[str] = set()
    for edge in edges:
        edge_list.append(_row_to_edge_dict(edge))
        for ep_name in (edge[1], edge[4]):
            if ep_name not in name_to_id:
                endpoint_names.add(ep_name)
            if ep_name in name_to_id:
                hop1_ids.add(name_to_id[ep_name])

    # Batch lookup for endpoint entities (H5 fix: single query instead of N+1)
    if endpoint_names:
        ep_placeholders = ",".join("?" * len(endpoint_names))
        ep_rows = conn.execute(
            f"SELECT id, name, entity_type, mentions FROM kg_entities WHERE name IN ({ep_placeholders})",
            list(endpoint_names),
        ).fetchall()
        for ep_row in ep_rows:
            name_to_id[ep_row[1]] = ep_row[0]
            endpoint_entities.append(ep_row)
            hop1_ids.add(ep_row[0])

    return edge_list, endpoint_entities, hop1_ids, name_to_id


def _assemble_2hop(
    conn,
    hop1_ids: set,
    entity_map: dict,
    limit: int,
    as_of: str | None,
) -> list:
    """Find 2-hop neighbors of the 1-hop entities and return their
    edges. Capped at 10 new 2-hop entity IDs and ``limit`` edges.

    Extracted 2026-06-22 from graph_search().
    """
    if not hop1_ids:
        return []
    already_ids = set(entity_map.keys())
    hop1_list = list(hop1_ids)
    placeholders = ",".join("?" * len(hop1_list))
    temporal_clause, temporal_params = _temporal_edge_clause(as_of)
    hop2_raw = conn.execute(
        f"""SELECT DISTINCT
                CASE WHEN e.source_id IN ({placeholders}) THEN e.target_id ELSE e.source_id END
            FROM kg_edges e
            WHERE (e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders}))
            {temporal_clause}""",
        hop1_list + hop1_list + hop1_list + temporal_params,
    ).fetchall()
    hop2_ids = [r[0] for r in hop2_raw if r[0] not in already_ids][:10]
    if not hop2_ids:
        return []
    placeholders2 = ",".join("?" * len(hop2_ids))
    hop2_entities = conn.execute(
        f"""SELECT id, name, entity_type, mentions
            FROM kg_entities
            WHERE id IN ({placeholders2})""",
        hop2_ids,
    ).fetchall()
    for e in hop2_entities:
        if e[0] not in entity_map:
            entity_map[e[0]] = _row_to_entity_dict(e)
    hop2_edges = conn.execute(
        f"""SELECT e.id, s.name, s.entity_type, e.relation,
                   t.name, t.entity_type, e.weight
            FROM kg_edges e
            JOIN kg_entities s ON e.source_id = s.id
            JOIN kg_entities t ON e.target_id = t.id
            WHERE (e.source_id IN ({placeholders2}) OR e.target_id IN ({placeholders2}))
            {temporal_clause}
            ORDER BY e.weight DESC
            LIMIT ?""",
        hop2_ids + hop2_ids + temporal_params + [limit],
    ).fetchall()
    return [_row_to_edge_dict(edge) for edge in hop2_edges]


def graph_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    max_hops: int = 2,
    as_of: str | None = None,
) -> dict:
    """Search the knowledge graph for entities matching the query.

    Returns entities with their edges, ranked by relevance.
    Temporal decay is applied: newer entities rank higher.

    as_of: ISO-8601 timestamp. When set, only edges that were valid
    at that point in time are returned (valid_at <= as_of AND
    (invalid_at IS NULL OR invalid_at >= as_of)).

    Decomposed 2026-06-22: entity matching, 1-hop assembly, 2-hop
    assembly, and row-dict shaping are now named helpers. The
    orchestrator reads as a 4-step pipeline.
    """
    import sys

    if not sys.modules["knowledge_graph"].KG_ENABLED:
        return {"entities": [], "edges": []}

    query_lower = query.lower().strip()
    cache_key = (query_lower, limit, max_hops, as_of)
    cached = _graph_cache_get(cache_key)
    if cached is not None:
        return cached

    entities = _match_query_entities(conn, query_lower, limit)
    if not entities:
        return {"entities": [], "edges": []}

    entity_ids = [e[0] for e in entities]
    entity_map = {e[0]: _row_to_entity_dict(e) for e in entities}

    edge_list, endpoint_entities, hop1_ids, _name_to_id = _assemble_1hop(
        conn, entities, entity_ids, limit, as_of
    )
    # Merge in 1-hop endpoints so callers see neighbor entities.
    for ep in endpoint_entities:
        if ep[0] not in entity_map:
            entity_map[ep[0]] = _row_to_entity_dict(ep)

    if max_hops >= 2:
        edge_list.extend(_assemble_2hop(conn, hop1_ids, entity_map, limit, as_of))

    # Deduplicate edges between 1-hop and 2-hop expansions
    seen_edge_ids: set[int] = set()
    unique_edges = []
    for e in edge_list:
        if e["id"] not in seen_edge_ids:
            seen_edge_ids.add(e["id"])
            unique_edges.append(e)

    result = {"entities": list(entity_map.values()), "edges": unique_edges}
    _graph_cache_put(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Graph Stats
# ---------------------------------------------------------------------------


def graph_stats(conn: sqlite3.Connection) -> dict:
    """Return statistics about the knowledge graph."""
    import sys

    if not sys.modules["knowledge_graph"].KG_ENABLED:
        return {"enabled": False}

    try:
        entity_count = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]

        # Type distribution
        type_dist = {}
        for row in conn.execute(
            "SELECT entity_type, COUNT(*) FROM kg_entities GROUP BY entity_type"
        ).fetchall():
            type_dist[row[0]] = row[1]

        # Relation distribution
        rel_dist = {}
        for row in conn.execute(
            "SELECT relation, COUNT(*) FROM kg_edges GROUP BY relation"
        ).fetchall():
            rel_dist[row[0]] = row[1]

        # Most connected entities
        top_entities = conn.execute(
            """SELECT e.name, e.entity_type,
                      COALESCE(src.cnt, 0) + COALESCE(tgt.cnt, 0) as connections
               FROM kg_entities e
               LEFT JOIN (
                   SELECT source_id, COUNT(*) AS cnt
                   FROM kg_edges GROUP BY source_id
               ) src ON src.source_id = e.id
               LEFT JOIN (
                   SELECT target_id, COUNT(*) AS cnt
                   FROM kg_edges GROUP BY target_id
               ) tgt ON tgt.target_id = e.id
               ORDER BY connections DESC
               LIMIT 5"""
        ).fetchall()

        return {
            "enabled": True,
            "entity_count": entity_count,
            "edge_count": edge_count,
            "type_distribution": type_dist,
            "relation_distribution": rel_dist,
            "most_connected": [
                {"name": r[0], "type": r[1], "connections": r[2]} for r in top_entities
            ],
        }
    except sqlite3.OperationalError:
        return {"enabled": True, "error": "KG tables not initialized"}


# ---------------------------------------------------------------------------
# DB-lifecycle wrappers (T3-item3: push conn mgmt out of MCP layer)
# ---------------------------------------------------------------------------


def graph_search_db(
    db_path: str | Path, query: str, limit: int = 10, max_hops: int = 2
) -> dict:
    """graph_search with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_kg_schema(conn)
    try:
        return graph_search(conn, query, limit=limit, max_hops=max_hops)
    finally:
        safe_close_db(conn)


def graph_stats_db(db_path: str | Path) -> dict:
    """graph_stats with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_kg_schema(conn)
    try:
        return graph_stats(conn)
    finally:
        safe_close_db(conn)


def index_kg_for_memory_db(db_path: str | Path, memory_id: str, content: str) -> dict:
    """index_kg_for_memory + index_facts_for_memory with connection lifecycle managed."""
    from memory_common import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    ensure_kg_schema(conn)
    try:
        result = index_kg_for_memory(conn, memory_id, content)
        from fact import ensure_facts_schema, index_facts_for_memory

        ensure_facts_schema(conn)
        index_facts_for_memory(conn, memory_id, content)
        conn.commit()
        return result
    finally:
        safe_close_db(conn)
