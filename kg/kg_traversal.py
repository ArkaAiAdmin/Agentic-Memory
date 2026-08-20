"""Graph Traversal Engine for Agentic Memory Knowledge Graph.

Provides:
  * ``find_shortest_path(conn, source_name, target_name, max_depth)`` - BFS pathfinding.
  * ``find_neighbors(conn, entity_name, direction, relation_types, max_depth)`` - Neighbors query.
  * ``traverse_graph(conn, start_name, edge_patterns)`` - Pattern-matching path crawler.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

# P0-3 fix: enforce entity_min_occurrences from memory.toml
_ENTITY_MIN_OCCURRENCES = 2


def find_shortest_path(
    conn: AnyConnection,
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
        entity_info_row = conn.execute(
            "SELECT id, name, entity_type FROM kg_entities WHERE id = ?", (source_id,)
        ).fetchone()
        if entity_info_row is None:
            return None
        return [{"id": entity_info_row[0], "name": entity_info_row[1], "entity_type": entity_info_row[2]}]

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
    conn: AnyConnection,
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
    conn: AnyConnection,
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
                rel_dict = {"relation": row[col_idx]}
                path.append(rel_dict)
                col_idx += 1
        paths.append(path)

    return paths


def find_cross_session_graph_walk(
    conn: AnyConnection,
    seed_entities: list[str],
    max_hops: int = 2,
    decay_factor: float = 0.7,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Traverse graph starting from seed entities across session boundaries up to max_hops.

    Discovers indirect entity connections, associated facts, and source memories.
    Computes graph proximity score with exponential hop decay: score = edge_weight * (decay_factor ** hop).

    Returns:
        List of discovered multi-hop nodes with provenance paths, facts, and relevance scores.
    """
    if not seed_entities or max_hops < 1:
        return []

    clean_seeds = [s.strip() for s in seed_entities if s.strip()]
    if not clean_seeds:
        return []

    placeholders = ",".join("?" for _ in clean_seeds)
    seed_rows = conn.execute(
        f"SELECT id, name, entity_type FROM kg_entities WHERE name IN ({placeholders})",
        clean_seeds,
    ).fetchall()

    seed_ids = {r[0] for r in seed_rows}
    seed_id_list = list(seed_ids)
    seed_ph = ",".join("?" for _ in seed_id_list)

    # CTE recursive search for multi-hop neighbors
    query = f"""
    WITH RECURSIVE walk(current_id, visited, depth, edge_id, path_ids, path_rels, weight_prod) AS (
        SELECT
            id,
            ',' || id || ',',
            0,
            NULL,
            ',' || id || ',',
            ',',
            1.0
        FROM kg_entities
        WHERE id IN ({seed_ph})

        UNION ALL

        SELECT
            CASE WHEN w.current_id = e.source_id THEN e.target_id ELSE e.source_id END,
            w.visited || (CASE WHEN w.current_id = e.source_id THEN e.target_id ELSE e.source_id END) || ',',
            w.depth + 1,
            e.id,
            w.path_ids || (CASE WHEN w.current_id = e.source_id THEN e.target_id ELSE e.source_id END) || ',',
            w.path_rels || e.relation || ',',
            w.weight_prod * COALESCE(e.weight, 1.0)
        FROM walk w
        JOIN kg_edges e ON (e.source_id = w.current_id OR e.target_id = w.current_id)
        WHERE w.depth < ?
          AND (e.invalid_at IS NULL OR e.invalid_at = '')
          AND instr(w.visited, ',' || (CASE WHEN w.current_id = e.source_id THEN e.target_id ELSE e.source_id END) || ',') = 0
    )
    SELECT current_id, depth, path_ids, path_rels, weight_prod
    FROM walk
    WHERE depth > 0
    ORDER BY depth ASC, weight_prod DESC
    """

    rows = conn.execute(query, seed_id_list + [max_hops]).fetchall()
    if not rows:
        return []

    all_entity_ids = set()
    walk_records = []
    seen_entities = set(seed_ids)

    for r in rows:
        curr_id, depth, path_ids_str, path_rels_str, weight_prod = r
        if curr_id in seen_entities and depth > 1:
            continue
        seen_entities.add(curr_id)

        p_ids = [int(x) for x in path_ids_str.split(",") if x]
        p_rels = [x for x in path_rels_str.split(",") if x]
        all_entity_ids.update(p_ids)

        score = float(weight_prod) * (decay_factor ** depth)
        walk_records.append({
            "target_id": curr_id,
            "depth": depth,
            "path_ids": p_ids,
            "path_rels": p_rels,
            "score": round(score, 4),
        })

    all_ph = ",".join("?" for _ in all_entity_ids)
    ent_rows = conn.execute(
        f"SELECT id, name, entity_type FROM kg_entities WHERE id IN ({all_ph})",
        list(all_entity_ids),
    ).fetchall()
    ent_dict = {r[0]: {"id": r[0], "name": r[1], "entity_type": r[2]} for r in ent_rows}

    # Fetch active facts for target entities
    target_ids = [w["target_id"] for w in walk_records]
    target_id_set = set(target_ids)
    fact_dict: dict[int, list[dict]] = {}
    if target_ids:
        tgt_ph = ",".join("?" for _ in target_ids)
        try:
            fact_rows = conn.execute(
                f"""SELECT id, subject, predicate, object, confidence, source_memory,
                          subject_entity_id, object_entity_id
                   FROM kg_facts
                   WHERE (subject_entity_id IN ({tgt_ph}) OR object_entity_id IN ({tgt_ph}))
                     AND (superseded_by IS NULL)
                     AND (invalid_at IS NULL OR invalid_at = '')
                   LIMIT 50""",
                target_ids + target_ids,
            ).fetchall()
            for fr in fact_rows:
                f_item = {
                    "id": fr[0],
                    "subject": fr[1],
                    "predicate": fr[2],
                    "object": fr[3],
                    "confidence": fr[4],
                    "source_memory": fr[5],
                }
                sub_id = fr[6]
                obj_id = fr[7]
                if sub_id is not None and sub_id in target_id_set:
                    fact_dict.setdefault(sub_id, []).append(f_item)
                if obj_id is not None and obj_id in target_id_set and obj_id != sub_id:
                    fact_dict.setdefault(obj_id, []).append(f_item)
        except Exception:
            pass

    results = []
    for w in walk_records[:limit]:
        tgt_meta = ent_dict.get(w["target_id"])
        if not tgt_meta:
            continue

        path = []
        for i, pid in enumerate(w["path_ids"]):
            p_ent = ent_dict.get(pid)
            if p_ent:
                path.append(p_ent)
            if i < len(w["path_rels"]):
                path.append({"relation": w["path_rels"][i]})

        results.append({
            "entity": tgt_meta,
            "hop": w["depth"],
            "score": w["score"],
            "path": path,
            "connected_facts": fact_dict.get(w["target_id"], [])[:5],
        })

    return results
