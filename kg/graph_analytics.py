"""Pure-Python graph analytics for the agentic-memory Knowledge Graph.

Provides PageRank and Degree centrality calculation and updates the entity database.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)


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
            out_degree[src] += w or 1.0

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
