"""Graph Traversal Engine for Agentic Memory Knowledge Graph.

Provides:
  * ``find_shortest_path(conn, source_name, target_name, max_depth)`` - BFS pathfinding.
  * ``find_neighbors(conn, entity_name, direction, relation_types, max_depth)`` - Neighbors query.
  * ``traverse_graph(conn, start_name, edge_patterns)`` - Pattern-matching path crawler.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


def find_shortest_path(
    conn: sqlite3.Connection,
    source_name: str,
    target_name: str,
    max_depth: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Find the shortest path between two entities by name using a Recursive CTE BFS.

    Returns:
        List of dicts alternating between entities and relations:
        [
            {"id": 1, "name": "A", "entity_type": "concept"},
            {"relation": "defines"},
            {"id": 2, "name": "B", "entity_type": "code"}
        ]
        or None if no path is found.
    """
    # 1. Resolve source and target IDs
    source_row = conn.execute(
        "SELECT id FROM kg_entities WHERE name = ?", (source_name,)
    ).fetchone()
    target_row = conn.execute(
        "SELECT id FROM kg_entities WHERE name = ?", (target_name,)
    ).fetchone()

    if not source_row or not target_row:
        return None

    source_id = source_row[0]
    target_id = target_row[0]

    if source_id == target_id:
        entity_info = conn.execute(
            "SELECT id, name, entity_type FROM kg_entities WHERE id = ?", (source_id,)
        ).fetchone()
        return [{"id": entity_info[0], "name": entity_info[1], "entity_type": entity_info[2]}]

    # 2. Run recursive CTE to find shortest path of IDs and relations
    # path_ids matches: ,id1,id2,id3,
    # path_rels matches: ,relation1,relation2,
    query = """
    WITH RECURSIVE bfs(entity_id, visited, depth, path_ids, path_rels) AS (
        SELECT
            id,
            ',' || id || ',',
            0,
            ',' || id || ',',
            ','
        FROM kg_entities
        WHERE id = :source_id

        UNION ALL

        SELECT
            e.target_id,
            bfs.visited || e.target_id || ',',
            bfs.depth + 1,
            bfs.path_ids || e.target_id || ',',
            bfs.path_rels || e.relation || ','
        FROM bfs
        JOIN kg_edges e ON e.source_id = bfs.entity_id
        WHERE bfs.depth < :max_depth
          AND instr(bfs.visited, ',' || e.target_id || ',') = 0
          AND (e.invalid_at IS NULL OR e.invalid_at = '')
    )
    SELECT path_ids, path_rels, depth
    FROM bfs
    WHERE entity_id = :target_id
    ORDER BY depth ASC
    LIMIT 1
    """

    row = conn.execute(
        query,
        {
            "source_id": source_id,
            "target_id": target_id,
            "max_depth": max_depth,
        },
    ).fetchone()

    if not row:
        return None

    path_ids_str, path_rels_str, _ = row

    # Parse path IDs and relations
    path_ids = [int(x) for x in path_ids_str.split(",") if x]
    path_rels = [x for x in path_rels_str.split(",") if x]

    # Bulk-fetch all entity metadata for the path to avoid N+1 queries
    placeholders = ",".join("?" for _ in path_ids)
    entities_rows = conn.execute(
        f"SELECT id, name, entity_type FROM kg_entities WHERE id IN ({placeholders})",
        path_ids,
    ).fetchall()

    entities_map = {r[0]: {"id": r[0], "name": r[1], "entity_type": r[2]} for r in entities_rows}

    # Reconstruct the alternating list
    result = []
    for i, pid in enumerate(path_ids):
        ent_meta = entities_map.get(pid)
        if not ent_meta:
            return None  # Integrity error: entity missing
        result.append(ent_meta)
        if i < len(path_rels):
            result.append({"relation": path_rels[i]})

    return result


def find_neighbors(
    conn: sqlite3.Connection,
    entity_name: str,
    direction: str = "out",
    relation_types: Optional[List[str]] = None,
    max_depth: int = 1,
) -> List[Dict[str, Any]]:
    """Retrieve neighbors for a starting entity name up to a given depth.

    Args:
        conn: SQLite connection.
        entity_name: Start entity name.
        direction: "out" | "in" | "both".
        relation_types: Optional filter list of relation type strings.
        max_depth: Depth of neighborhood crawl (default 1).

    Returns:
        List of edges representing neighbor relationships:
        [
            {
                "source": {"name": "A", "entity_type": "concept"},
                "target": {"name": "B", "entity_type": "code"},
                "relation": "defines",
                "weight": 1.0,
                "depth": 1
            }
        ]
    """
    start_row = conn.execute(
        "SELECT id FROM kg_entities WHERE name = ?", (entity_name,)
    ).fetchone()
    if not start_row:
        return []

    start_id = start_row[0]

    # Validate inputs
    if direction not in ("out", "in", "both"):
        direction = "out"
    if max_depth < 1:
        max_depth = 1

    # CTE query to traverse edges. We track depth and avoid cycles.
    query = """
    WITH RECURSIVE neighbors(entity_id, visited, depth, edge_id, source_id, target_id, relation, weight) AS (
        SELECT
            id,
            ',' || id || ',',
            0,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL
        FROM kg_entities
        WHERE id = :start_id

        UNION ALL

        SELECT
            CASE
                WHEN :direction = 'out' THEN e.target_id
                WHEN :direction = 'in' THEN e.source_id
                ELSE (CASE WHEN n.entity_id = e.source_id THEN e.target_id ELSE e.source_id END)
            END as next_id,
            n.visited || (
                CASE
                    WHEN :direction = 'out' THEN e.target_id
                    WHEN :direction = 'in' THEN e.source_id
                    ELSE (CASE WHEN n.entity_id = e.source_id THEN e.target_id ELSE e.source_id END)
                END
            ) || ',',
            n.depth + 1,
            e.id,
            e.source_id,
            e.target_id,
            e.relation,
            e.weight
        FROM neighbors n
        JOIN kg_edges e ON (
            (:direction = 'out' AND e.source_id = n.entity_id) OR
            (:direction = 'in' AND e.target_id = n.entity_id) OR
            (:direction = 'both' AND (e.source_id = n.entity_id OR e.target_id = n.entity_id))
        )
        WHERE n.depth < :max_depth
          AND (e.invalid_at IS NULL OR e.invalid_at = '')
          AND instr(n.visited, ',' || (
              CASE
                  WHEN :direction = 'out' THEN e.target_id
                  WHEN :direction = 'in' THEN e.source_id
                  ELSE (CASE WHEN n.entity_id = e.source_id THEN e.target_id ELSE e.source_id END)
              END
          ) || ',') = 0
    )
    SELECT DISTINCT edge_id, source_id, target_id, relation, weight, depth
    FROM neighbors
    WHERE edge_id IS NOT NULL
    """

    rows = conn.execute(
        query,
        {
            "start_id": start_id,
            "direction": direction,
            "max_depth": max_depth,
        },
    ).fetchall()

    if not rows:
        return []

    # Filter relation types if provided (best done in Python since relations list is small)
    if relation_types:
        rel_set = {r.lower() for r in relation_types}
        rows = [r for r in rows if r[3].lower() in rel_set]

    # Fetch unique entity IDs to resolve metadata in one query
    entity_ids = set()
    for r in rows:
        entity_ids.add(r[1])
        entity_ids.add(r[2])

    placeholders = ",".join("?" for _ in entity_ids)
    entities_rows = conn.execute(
        f"SELECT id, name, entity_type FROM kg_entities WHERE id IN ({placeholders})",
        list(entity_ids),
    ).fetchall()
    entities_map = {r[0]: {"name": r[1], "entity_type": r[2]} for r in entities_rows}

    results = []
    for r in rows:
        edge_id, src_id, tgt_id, rel, weight, depth = r
        src_meta = entities_map.get(src_id)
        tgt_meta = entities_map.get(tgt_id)
        if src_meta and tgt_meta:
            results.append(
                {
                    "source": src_meta,
                    "target": tgt_meta,
                    "relation": rel,
                    "weight": weight,
                    "depth": depth,
                }
            )

    return results


def traverse_graph(
    conn: sqlite3.Connection,
    start_name: str,
    edge_patterns: List[str],
) -> List[List[Dict[str, Any]]]:
    """Crawl the graph following a specific sequence of relation types.

    Example:
        edge_patterns = ["defines", "imports"]
        Returns all paths: StartNode -[defines]-> MidNode -[imports]-> EndNode

    Returns:
        List of paths, where each path is a list of dicts alternating between entities and relations.
    """
    if not edge_patterns:
        return []

    # We dynamically build the query with joins to maximize SQLite planner speed.
    # We join N edges and N entities.
    num_joins = len(edge_patterns)
    select_parts = ["e0.id as id0, e0.name as name0, e0.entity_type as type0"]
    join_parts = []
    params = []

    for idx, rel in enumerate(edge_patterns):
        select_parts.append(f"edge{idx}.relation as rel{idx}")
        select_parts.append(f"e{idx+1}.id as id{idx+1}, e{idx+1}.name as name{idx+1}, e{idx+1}.entity_type as type{idx+1}")

        join_parts.append(
            f"JOIN kg_edges edge{idx} ON edge{idx}.source_id = e{idx}.id AND edge{idx}.relation = ? AND (edge{idx}.invalid_at IS NULL OR edge{idx}.invalid_at = '')"
        )
        join_parts.append(
            f"JOIN kg_entities e{idx+1} ON e{idx+1}.id = edge{idx}.target_id"
        )
        params.append(rel)

    select_clause = ", ".join(select_parts)
    joins_clause = "\n    ".join(join_parts)

    query = f"""
    SELECT
        {select_clause}
    FROM kg_entities e0
    {joins_clause}
    WHERE e0.name = ?
    """
    params.append(start_name)

    rows = conn.execute(query, params).fetchall()

    paths = []
    for row in rows:
        path = []
        # Reconstruct path from row values
        # Column structure:
        # idx 0,1,2: Entity0 (id, name, type)
        # idx 3: Rel0
        # idx 4,5,6: Entity1 (id, name, type)
        # idx 7: Rel1
        # ...
        col_idx = 0
        for i in range(num_joins + 1):
            ent = {
                "id": row[col_idx],
                "name": row[col_idx+1],
                "entity_type": row[col_idx+2],
            }
            path.append(ent)
            col_idx += 3
            if i < num_joins:
                rel = {"relation": row[col_idx]}
                path.append(rel)
                col_idx += 1
        paths.append(path)

    return paths
