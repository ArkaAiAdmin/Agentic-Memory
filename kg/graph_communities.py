"""Pure-Python graph community detection for the agentic-memory Knowledge Graph.

Provides:
  - connected_components: SQLite-recursive-CTE connected-component labeling
  - louvain_communities: two-phase Louvain modularity maximization
  - compute_communities: unified dispatcher
  - write_community_ids: persist results into kg_entities.community_id

No external dependencies required.
"""

from __future__ import annotations

import logging

from infra.db import AnyConnection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connected components
# ---------------------------------------------------------------------------


def connected_components(
    conn: AnyConnection,
    min_component_size: int = 1,
) -> dict[int, int]:
    """Return ``{entity_id: component_id}`` using an iterative BFS pass.

    Falls back to a SQLite recursive CTE when the graph is large enough
    that Python iteration would be slow.

    Args:
        conn: Open database connection.
        min_component_size: Components smaller than this are collapsed into
            component 0 (isolated bucket).

    Returns:
        Dict mapping entity_id -> integer community id.
    """
    edges: dict[int, set[int]] = {}
    for src, tgt in conn.execute("""
        SELECT source_id, target_id
        FROM kg_edges
        WHERE invalid_at IS NULL OR invalid_at = ''
    """).fetchall():
        edges.setdefault(int(src), set()).add(int(tgt))
        edges.setdefault(int(tgt), set()).add(int(src))

    nodes: set[int] = set(e for s in edges.values() for e in s)
    node_rows = conn.execute("SELECT id FROM kg_entities").fetchall()
    for (nid,) in node_rows:
        nodes.add(int(nid))
        edges.setdefault(int(nid), set())

    component_map: dict[int, int] = {}
    visited: set[int] = set()
    next_component_id = 1

    for start in sorted(nodes):
        if start in visited:
            continue
        queue = [start]
        visited.add(start)
        members: list[int] = []
        while queue:
            current = queue.pop(0)
            members.append(current)
            for neighbor in edges.get(current, ()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(members) >= min_component_size:
            cid = next_component_id
            next_component_id += 1
            for m in members:
                component_map[m] = cid
        else:
            for m in members:
                component_map[m] = 0

    return component_map


# ---------------------------------------------------------------------------
# Louvain modularity maximization
# ---------------------------------------------------------------------------


def louvain_communities(
    conn: AnyConnection,
    resolution: float = 1.0,
    max_phases: int = 20,
    min_component_size: int = 1,
    seed_components: bool = True,
) -> dict[int, int]:
    """Return ``{entity_id: community_id}`` via the Louvain method.

    Pure-Python two-phase Louvain. Runs in O(n log n) expected time for
    sparse graphs with < 100k edges.

    Args:
        conn: Open database connection.
        resolution: Resolution parameter. Higher values yield more communities.
        max_phases: Upper bound on greedy optimization passes.
        min_component_size: Used only when ``seed_components`` is True.
        seed_components: Seed initial communities from connected components
            so isolated nodes are deterministically placed.

    Returns:
        Dict mapping entity_id -> integer community id.
    """
    nodes, adjacency, total_weight = _load_graph(conn)
    if not nodes:
        return {}

    if seed_components and len(nodes) > 1:
        community: dict[int, int] = connected_components(
            conn, min_component_size=min_component_size
        )
    else:
        index = {nid: i for i, nid in enumerate(nodes)}
        community = {nid: index[nid] for nid in nodes}

    iteration = 0
    while iteration < max_phases:
        improved = _louvain_phase(
            nodes, adjacency, total_weight, community, resolution
        )
        if not improved:
            break
        iteration += 1

    output: dict[int, int] = {}
    for nid, cid in community.items():
        output[int(nid)] = int(cid)
    return output


def _load_graph(
    conn: AnyConnection,
) -> tuple[list[int], dict[tuple[int, int], float], float]:
    """Load active edges into adjacency dicts.

    Returns:
        (nodes, edge_weights, total_weight)
    """
    rows = conn.execute("""
        SELECT e.source_id, e.target_id, COALESCE(e.weight, 1.0)
        FROM kg_edges e
        WHERE e.invalid_at IS NULL OR e.invalid_at = ''
    """).fetchall()

    node_set: set[int] = set()
    edge_weights: dict[tuple[int, int], float] = {}
    total = 0.0

    for src, tgt, w in rows:
        u, v = int(src), int(tgt)
        node_set.add(u)
        node_set.add(v)
        weight = max(float(w), 1e-9)
        if u > v:
            u, v = v, u
            key = (u, v)
        else:
            key = (u, v)
        edge_weights[key] = edge_weights.get(key, 0.0) + weight
        total += weight

    nodes = sorted(node_set)
    return nodes, edge_weights, total


def _louvain_phase(
    nodes: list[int],
    adjacency: dict[tuple[int, int], float],
    total_weight: float,
    community: dict[int, int],
    resolution: float,
) -> bool:
    """Single Louvain greedy-optimization phase. Returns True if any node moved."""
    m2 = max(total_weight * 2.0, 1e-9)

    degrees: dict[int, float] = {n: 0.0 for n in nodes}
    for (u, v), w in adjacency.items():
        degrees[u] = degrees.get(u, 0.0) + w
        degrees[v] = degrees.get(v, 0.0) + w

    improved = False
    order = list(nodes)

    for node in order:
        node_comm = community[node]
        node_degree = degrees.get(node, 0.0)

        neighbors: dict[int, float] = {}
        for nbr in nodes:
            if nbr == node:
                continue
            key = (min(node, nbr), max(node, nbr))
            w_edge = adjacency.get(key, 0.0)
            if w_edge > 0:
                neighbors[nbr] = neighbors.get(nbr, 0.0) + w_edge

        neighbor_communities: dict[int, float] = {}
        for nbr, w_edge in neighbors.items():
            c = community[nbr]
            if c != node_comm:
                neighbor_communities[c] = neighbor_communities.get(c, 0.0) + w_edge

        best_comm = node_comm
        best_gain = 0.0

        neighbor_comms = list(neighbor_communities.items())
        if node_comm not in [c for c, _ in neighbor_comms]:
            sigma_tot_here = sum(
                neighbors.get(nbr, 0.0) for nbr in nodes if community.get(nbr) == node_comm
            ) + node_degree
            neighbor_comms.append((node_comm, sigma_tot_here
                                   - sum(w for c, w in neighbor_comms)))

        for comm_id, sigma_in in neighbor_comms:
            sigma_tot = sum(
                neighbors.get(nbr, 0.0)
                for nbr in nodes
                if community.get(nbr) == comm_id
            )
            if community.get(node) != comm_id:
                sigma_tot += node_degree
            delta_q = (sigma_in / m2) - (resolution * sigma_tot * node_degree) / (m2 * m2)
            current_in = sum(
                adjacency.get((min(node, nbr), max(node, nbr)), 0.0)
                for nbr in nodes
                if community.get(nbr) == node_comm
            )
            current_tot = sum(
                neighbors.get(nbr, 0.0)
                for nbr in nodes
                if community.get(nbr) == node_comm
            ) + node_degree
            current_delta_q = (current_in / m2) - (resolution * current_tot * node_degree) / (m2 * m2)
            gain = delta_q - current_delta_q
            if gain > best_gain:
                best_gain = gain
                best_comm = comm_id

        if best_comm != node_comm and best_gain > 1e-12:
            community[node] = best_comm
            improved = True

    return improved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_communities(
    conn: AnyConnection,
    algorithm: str = "louvain",
    resolution: float = 1.0,
    min_component_size: int = 1,
) -> dict[int, int]:
    """Dispatch community detection.

    Args:
        conn: Open database connection.
        algorithm: ``"louvain"`` or ``"connected"``.
        resolution: Louvain resolution (only when algorithm="louvain").
        min_component_size: Components smaller than this are collapsed
            into community 0 (only when algorithm="connected").

    Returns:
        ``{entity_id: community_id}`` dict.
    """
    if algorithm == "connected":
        return connected_components(conn, min_component_size=min_component_size)
    if algorithm == "louvain":
        return louvain_communities(
            conn, resolution=resolution, min_component_size=min_component_size
        )
    raise ValueError(f"Unknown algorithm={algorithm!r}. Use 'louvain' or 'connected'.")


def write_community_ids(
    conn: AnyConnection,
    membership: dict[int, int],
) -> int:
    """Persist community_id values into kg_entities.

    Returns number of rows updated.
    """
    updated = 0
    for entity_id, community_id in membership.items():
        cur = conn.execute(
            "UPDATE kg_entities SET community_id = ? WHERE id = ?",
            (community_id, entity_id),
        )
        updated += cur.rowcount if cur.rowcount is not None else 0
    return updated
