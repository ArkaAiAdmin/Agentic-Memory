"""Pure-Python graph analytics for the agentic-memory Knowledge Graph.

Provides PageRank, Betweenness centrality calculation, and DB updates.
"""

from __future__ import annotations

import collections
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PageRank
# ---------------------------------------------------------------------------


def compute_pagerank(
    conn: AnyConnection,
    damping: float = 0.85,
    max_iters: int = 100,
    tol: float = 1e-6,
) -> dict[int, float]:
    """Compute PageRank scores for all active entities in the Knowledge Graph.

    Returns:
        Dict of {entity_id: pagerank_score}.
    """
    # 1. Fetch all entity IDs
    nodes = [r[0] for r in conn.execute("SELECT id FROM kg_entities").fetchall()]
    N = len(nodes)
    if N == 0:
        return {}

    # Initialize pagerank scores
    pr = {node_id: 1.0 / N for node_id in nodes}

    # 2. Fetch active edges and compute out-degrees
    edges = conn.execute(
        "SELECT source_id, target_id, weight FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
    ).fetchall()

    adj: dict[int, list[int]] = {nid: [] for nid in nodes}
    out_degree: dict[int, float] = {nid: 0.0 for nid in nodes}

    for src, tgt, w in edges:
        # Filter nodes that might have been deleted but have stale edges
        if src in adj and tgt in adj:
            adj[src].append(tgt)
            try:
                w_f = float(w)
            except (TypeError, ValueError):
                w_f = 1.0
            out_degree[src] += w_f

    dangling_nodes = [nid for nid in nodes if out_degree[nid] == 0.0]

    # 3. Power iteration
    for i in range(max_iters):
        next_pr = {nid: 0.0 for nid in nodes}

        # Distribute dangling node ranks
        dangling_sum = sum(pr[dn] for dn in dangling_nodes)
        dangling_share = dangling_sum / N

        for nid in nodes:
            next_pr[nid] += dangling_share

        # Distribute ranks along edges
        for src, targets in adj.items():
            if out_degree[src] > 0:
                share = pr[src] / out_degree[src]
                for tgt in targets:
                    next_pr[tgt] += share

        # Apply damping factor and calculate L1 difference
        diff = 0.0
        base_share = (1.0 - damping) / N
        for nid in nodes:
            next_pr[nid] = base_share + damping * next_pr[nid]
            diff += abs(next_pr[nid] - pr[nid])

        pr = next_pr

        if diff < tol:
            logger.debug("PageRank converged in %d iterations (diff=%f)", i + 1, diff)
            break

    return pr


def update_graph_analytics(conn: AnyConnection) -> dict[str, Any]:
    """Compute PageRank scores and update the centrality column in kg_entities.

    Returns:
        Dict with status statistics.
    """
    try:
        pr = compute_pagerank(conn)
        updated = 0
        for entity_id, score in pr.items():
            conn.execute(
                "UPDATE kg_entities SET centrality = ?, updated_at = datetime('now') WHERE id = ?",
                (round(score, 12), entity_id),
            )
            updated += 1
        return {"entities_updated": updated}
    except Exception as e:
        logger.exception("Failed to update graph centrality: %s", e)
        return {"entities_updated": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Betweenness centrality — Brandes algorithm
# ---------------------------------------------------------------------------


def compute_betweenness(
    conn: AnyConnection,
    normalized: bool = True,
) -> dict[int, float]:
    """Compute betweenness centrality for all active KG entities (Brandes algorithm).

    O(V * (V + E)) worst case, fast for typical memory graphs (< 100k edges).
    Treats the KG as an undirected graph with weighted edges.

    Args:
        conn: Open database connection.
        normalized: If True, normalize scores by (N-1)*(N-2) for undirected
            graphs so the maximum possible score is 1.0.

    Returns:
        Dict of {entity_id: betweenness_score}.
    """
    nodes = [r[0] for r in conn.execute("SELECT id FROM kg_entities").fetchall()]
    if not nodes:
        return {}

    edges = conn.execute(
        "SELECT source_id, target_id, COALESCE(weight, 1.0) AS w FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
    ).fetchall()

    adj: dict[int, dict[int, float]] = {nid: {} for nid in nodes}
    for src, tgt, w in edges:
        if src in adj and tgt in adj:
            adj[src][tgt] = adj[src].get(tgt, 0.0) + float(w)
            adj[tgt][src] = adj[tgt].get(src, 0.0) + float(w)

    N = len(nodes)
    betweenness: dict[int, float] = {nid: 0.0 for nid in nodes}

    for s in nodes:
        S: list[int] = []
        P: dict[int, list[int]] = {n: [] for n in nodes}
        sigma: dict[int, float] = {n: 0.0 for n in nodes}
        sigma[s] = 1.0
        delta: dict[int, float] = {n: 0.0 for n in nodes}
        d: dict[int, int] = {n: -1 for n in nodes}
        d[s] = 0
        Q: collections.deque[int] = collections.deque([s])

        while Q:
            v = Q.popleft()
            S.append(v)
            for w_v, _ in adj.get(v, {}).items():
                if d[w_v] < 0:
                    Q.append(w_v)
                    d[w_v] = d[v] + 1
                if d[w_v] == d[v] + 1:
                    sigma[w_v] += sigma[v]
                    P[w_v].append(v)

        while S:
            w = S.pop()
            for v in P[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                betweenness[w] += delta[w]

    if normalized and N > 2:
        divisor = (N - 1) * (N - 2)
        for nid in nodes:
            betweenness[nid] /= max(divisor, 1)

    return betweenness


def update_betweenness(conn: AnyConnection, normalized: bool = True) -> dict[str, Any]:
    """Compute betweenness centrality and persist to kg_entities.betweenness.

    Returns:
        Dict with status statistics.
    """
    try:
        bw = compute_betweenness(conn, normalized=normalized)
        updated = 0
        for entity_id, score in bw.items():
            conn.execute(
                "UPDATE kg_entities SET betweenness = ?, updated_at = datetime('now') WHERE id = ?",
                (round(score, 12), entity_id),
            )
            updated += 1
        return {"entities_updated": updated}
    except Exception as e:
        logger.exception("Failed to update betweenness centrality: %s", e)
        return {"entities_updated": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Back-compat re-exports so prior importers keep working
# ---------------------------------------------------------------------------

__all__ = [
    "compute_pagerank",
    "update_graph_analytics",
    "compute_betweenness",
    "update_betweenness",
]
