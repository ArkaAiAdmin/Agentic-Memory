from __future__ import annotations
"""
Knowledge Graph MCP tools — graph_search, graph_stats, facts_search, facts_list, facts_stats,
graph_insights, graph_evolution.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401




from mcp_common import (
    _resolve_memory_dir,
    logger,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_instance import mcp
from typing import Any


# ---------------------------------------------------------------------------
# RBAC authorization helpers (mirror mcp_verbs.py — fail-open by design)
# ---------------------------------------------------------------------------


def _resolve_auth_db_path() -> str | None:
    """Resolve the DB path for RBAC authorization checks."""
    try:
        db_path = _resolve_memory_dir() / "memory.db"
        return str(db_path) if db_path and db_path.exists() else None
    except Exception:
        return None


def _get_principal_from_context() -> str | None:
    """Resolve the current principal from MCP request context (Phase 1)."""
    try:
        from agent_context import get_agent
        ctx = get_agent()
        principal_id = getattr(ctx, "principal_id", None)
        if principal_id:
            return str(principal_id)
        # AgentContext stores identifier as agent_id; fall back when
        # principal_id is not separately set (typical for local single-user mode).
        agent_id = getattr(ctx, "agent_id", None)
        if agent_id:
            return str(agent_id).lower()
    except (ImportError, Exception):
        pass
    return None


def _check_authorization(action: str, resource: str = "memory") -> str | None:
    """Check RBAC authorization. Returns error string if denied, None if allowed.

    Fail-open: returns None (allow) on any error or when no RBAC is configured.
    """
    try:
        from infra.authorizer import mcp_authorize, log_authorization_decision

        principal_id = _get_principal_from_context()
        db_path = _resolve_auth_db_path()

        allowed = mcp_authorize(
            principal_id=principal_id,
            action=action,
            resource=resource,
            db_path=db_path,
        )
        if not allowed:
            log_authorization_decision(
                principal_id=principal_id,
                action=action,
                resource=resource,
                allowed=False,
                db_path=db_path,
            )
            return _err(
                ErrorCode.AUTHORIZATION_DENIED,
                f"Not authorized for '{action}' on '{resource}'. "
                f"Principal '{principal_id or 'anonymous'}' lacks the required role.",
            )
        log_authorization_decision(
            principal_id=principal_id,
            action=action,
            resource=resource,
            allowed=True,
            db_path=db_path,
        )
        return None
    except Exception:
        return None


@mcp.tool()
@with_audit("memory_graph_search")
def memory_graph_search(query: str, limit: int = 10, max_hops: int = 2) -> str:
    """Search the knowledge graph for entities matching the query.

    Returns entities with their relations, ranked by mention count.
    Requires MEMORY_KNOWLEDGE_GRAPH=1 to be enabled.
    """
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    try:
        from knowledge_graph import graph_search_db

        result = graph_search_db(db_path, query, limit=limit, max_hops=max_hops)
        if not result["entities"]:
            return f"No graph results for '{query}'."
        lines = [
            f"**Graph results for '{query}'** ({len(result['entities'])} entities, {len(result['edges'])} edges):"
        ]
        for e in result["entities"]:
            lines.append(
                f"  - [{e['entity_type']}] {e['name']} (mentions={e['mentions']})"
            )
        if result["edges"]:
            lines.append("")
            lines.append("Edges:")
            for ed in result["edges"]:
                lines.append(
                    f"  {ed['source']} --[{ed['relation']}]--> {ed['target']} (weight={ed['weight']:.2f})"
                )
        return "\n".join(lines)
    except Exception as e:
        logger.exception("in memory_graph_search")
        return _err(ErrorCode.DB_ERROR, "in memory_graph_search")


@mcp.tool()
@with_audit("memory_graph_stats")
def memory_graph_stats() -> str:
    """Return statistics about the knowledge graph: entity/edge counts, type distribution, most connected entities."""
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    try:
        from knowledge_graph import graph_stats_db

        stats = graph_stats_db(db_path)
        if not stats.get("enabled"):
            return "Knowledge graph disabled."
        lines = [
            "**Knowledge Graph Stats**",
            f"  Entities: {stats.get('entity_count', 0)}",
            f"  Edges: {stats.get('edge_count', 0)}",
        ]
        if stats.get("type_distribution"):
            lines.append(
                "  Types: "
                + ", ".join(f"{k}={v}" for k, v in stats["type_distribution"].items())
            )
        if stats.get("relation_distribution"):
            lines.append(
                "  Relations: "
                + ", ".join(
                    f"{k}={v}" for k, v in stats["relation_distribution"].items()
                )
            )
        if stats.get("most_connected"):
            lines.append("  Most connected:")
            for e in stats["most_connected"]:
                lines.append(
                    f"    {e['name']} ({e['type']}) -- {e['connections']} edges"
                )
        return "\n".join(lines)
    except Exception as e:
        logger.exception("in memory_graph_stats")
        return _err(ErrorCode.DB_ERROR, "in memory_graph_stats")


@mcp.tool()
@with_audit("memory_facts_search")
def memory_facts_search(query: str, limit: int = 10) -> str:
    """Search extracted facts (SPO triples) matching the query.

    Returns facts with confidence scores. Locked facts are not affected
    by temporal decay. Requires MEMORY_KNOWLEDGE_GRAPH=1.
    """
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    try:
        from fact import facts_search_db

        results = facts_search_db(db_path, query, limit=limit)
        if not results:
            return f"No facts found for '{query}'."
        lines = [f"**Facts for '{query}'** ({len(results)} results):"]
        for f in results:
            lock = " [LOCKED]" if f["locked"] else ""
            lines.append(
                f"  {f['subject']} --[{f['predicate']}]--> {f['object']} "
                f"(conf={f['effective_confidence']:.3f}, mentions={f['mention_count']}){lock}"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_facts_search")
        return _err(ErrorCode.DB_ERROR, "in memory_facts_search")


@mcp.tool()
@with_audit("memory_facts_list")
def memory_facts_list(limit: int = 20, min_confidence: float = 0.0) -> str:
    """List all extracted facts above a confidence threshold, ordered by confidence.

    Requires MEMORY_KNOWLEDGE_GRAPH=1.
    """
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    try:
        from fact import facts_list_db

        results = facts_list_db(db_path, limit=limit, min_confidence=min_confidence)
        if not results:
            return "No facts extracted yet."
        lines = [f"**All Facts** ({len(results)} shown):"]
        for f in results:
            lock = " [LOCKED]" if f["locked"] else ""
            lines.append(
                f"  {f['subject']} --[{f['predicate']}]--> {f['object']} "
                f"(conf={f['confidence']:.3f}, mentions={f['mention_count']}){lock}"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_facts_list")
        return _err(ErrorCode.DB_ERROR, "in memory_facts_list")


@mcp.tool()
@with_audit("memory_facts_stats")
def memory_facts_stats() -> str:
    """Return statistics about extracted facts: total, locked, avg confidence, predicate distribution."""
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    try:
        from fact import facts_stats_db

        stats = facts_stats_db(db_path)
        lines = [
            "**Fact Extraction Stats**",
            f"  Total facts: {stats.get('total_facts', 0)}",
            f"  Locked facts: {stats.get('locked_facts', 0)}",
            f"  Avg confidence: {stats.get('avg_confidence', 0):.4f}",
        ]
        if stats.get("predicate_distribution"):
            lines.append(
                "  Predicates: "
                + ", ".join(
                    f"{k}={v}" for k, v in stats["predicate_distribution"].items()
                )
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_facts_stats")
        return _err(ErrorCode.DB_ERROR, "in memory_facts_stats")


@mcp.tool()
@with_audit("memory_graph_insights")
def memory_graph_insights(
    sample_size: int = 20,
    include_bridge: bool = True,
    conn=None,
) -> str:
    """Return graph analytics insights: density, modularity, avg path length, bridge nodes.

    Uses PageRank for importance ranking,     betweenness for bridge nodes, and
    connected components for community detection.
    """
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    try:
        from contextlib import nullcontext, AbstractContextManager
        from kg.graph_analytics import compute_pagerank
        from kg.graph_communities import connected_components

        out = ["Graph Analytics Insights"]
        if conn is not None:
            _conn_ctx: AbstractContextManager[Any] = nullcontext(conn)
        else:
            from infra.db import open_db
            target_base = _resolve_memory_dir()
            db_path = target_base / "memory.db"
            if not db_path.exists():
                return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
            _conn_ctx = open_db(db_path, row_factory=None)
        with _conn_ctx as conn:
            entity_row = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()
            entity_count = int(entity_row[0]) if entity_row and entity_row[0] is not None else 0
            edge_row = conn.execute(
                "SELECT COUNT(*) FROM kg_edges WHERE invalid_at IS NULL OR invalid_at = ''"
            ).fetchone()
            edge_count = int(edge_row[0]) if edge_row and edge_row[0] is not None else 0
            max_edges = max(entity_count * (entity_count - 1) / 2, 1e-9)
            density = min(edge_count / max_edges, 1.0)

            cc = connected_components(conn)
            community_count = len(set(cc.values()))
            community_sizes: dict[int, int] = {}
            for cid in cc.values():
                community_sizes[cid] = community_sizes.get(cid, 0) + 1
            largest_community = max(community_sizes.values()) if community_sizes else 0

            pr = compute_pagerank(conn)
            top_pr = sorted(pr.items(), key=lambda x: x[1], reverse=True)[:sample_size]

            avg_centrality = sum(pr.values()) / len(pr) if pr else 0.0

            betweenness: dict[int, float] = {}
            if include_bridge:
                try:
                    from kg.graph_analytics import compute_betweenness
                    betweenness = compute_betweenness(conn)
                except Exception as e:
                    logger.warning("Unhandled exception in memory_graph_insights: %s", e)
                    betweenness = {}
            top_betweenness = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:sample_size]

            sampled_paths = []
            try:
                sample_n = min(sample_size, entity_count)
                sample_ids = [nid for nid, _ in top_pr[:sample_n]] if top_pr else []
                if len(sample_ids) >= 2:
                    from kg.kg_traversal import find_shortest_path
                    for i in range(min(len(sample_ids) - 1, sample_size)):
                        try:
                            dist = find_shortest_path(
                                conn, source_name=str(sample_ids[i]), target_name=str(sample_ids[i + 1]), max_depth=4
                            )
                            if dist is not None:
                                sampled_paths.append(dist)
                        except Exception as e:
                            logger.warning("Unhandled exception in memory_graph_insights: %s", e)
            except Exception as e:
                logger.warning("Unhandled exception in memory_graph_insights: %s", e)
                sampled_paths = []

            avg_path = sum(len(p) for p in sampled_paths) / len(sampled_paths) if sampled_paths else None

            top_pr_names: list[tuple[int, float, str | None]] = []
            for eid, score in top_pr[:10]:
                nr = conn.execute("SELECT name FROM kg_entities WHERE id = ?", (eid,)).fetchone()
                top_pr_names.append((eid, score, nr[0] if nr else None))

            top_bw_names: list[tuple[int, float, str | None]] = []
            if include_bridge and top_betweenness:
                for eid, score in top_betweenness[:10]:
                    nr = conn.execute("SELECT name FROM kg_entities WHERE id = ?", (eid,)).fetchone()
                    top_bw_names.append((eid, score, nr[0] if nr else None))

        out.extend(
            [
                f"  Entities: {entity_count}",
                f"  Edges: {edge_count}",
                f"  Density: {density:.4f}",
                f"  Communities (components): {community_count}",
                f"  Largest community size: {largest_community}",
                f"  Avg PageRank: {avg_centrality:.6f}",
            ]
        )
        if avg_path is not None:
            out.append(f"  Avg sampled shortest-path length: {avg_path:.2f}")
        else:
            out.append("  Avg sampled shortest-path length: N/A (too few paths)")

        out.append("\nTop entities by PageRank:")
        for eid, score, name in top_pr_names:
            out.append(f"  {name or eid}: {score:.6f}")

        if top_bw_names:
            out.append("\nTop bridge entities (betweenness):")
            for eid, score, name in top_bw_names:
                out.append(f"  {name or eid}: {score:.6f}")

        return "\n".join(out)
    except Exception as e:
        logger.exception("in memory_graph_insights")
        return _err(ErrorCode.DB_ERROR, f"memory_graph_insights: {e}")


@mcp.tool()
@with_audit("memory_graph_evolution")
def memory_graph_evolution(since: str = "24h", limit: int = 5) -> str:
    """Return knowledge graph changes since a given time window.

    Args:
        since: Time window string parsed by graph_snapshots (e.g. "24h",
            "7d"). Falls back to comparing the two most recent snapshots.
        limit: Maximum number of diffs / top items to show.
    """
    from config import get_config

    if not getattr(get_config(), 'graph_evolution_tracking', True):
        return "Graph evolution tracking disabled. Set MEMORY_GRAPH_EVOLUTION_TRACKING=1 to enable."
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")
    auth_err = _check_authorization("read", "memory")
    if auth_err:
        return auth_err
    try:
        from infra.db import open_db
        import json as _json

        out = ["**Graph Evolution**"]
        with open_db(db_path, row_factory=None) as conn:
            rows = conn.execute(
                "SELECT id, captured_at, entity_count, edge_count, community_count, "
                "       avg_centrality, top_entities, new_entities, removed_entities "
                "FROM graph_snapshots ORDER BY captured_at DESC LIMIT ?",
                (limit * 2,),
            ).fetchall()

            if not rows:
                return "No graph snapshots captured yet. Graph snapshots are taken by the background worker."

            current = rows[0]
            out.append(f"Latest snapshot (id={current[0]}, captured_at={current[1]}):")
            out.extend(
                [
                    f"  Entities: {current[2]}",
                    f"  Edges: {current[3]}",
                    f"  Communities: {current[4]}",
                    f"  Avg centrality: {current[5]:.6f}" if current[5] is not None else "  Avg centrality: N/A",
                ]
            )
            try:
                top = _json.loads(current[6]) if current[6] else []
                if top:
                    out.append(f"  Top entities: {', '.join(e['name'] for e in top[:5])}")
            except Exception as e:
                logger.warning("Unhandled exception in memory_graph_evolution: %s", e)

            if len(rows) >= 2:
                previous = rows[-1]
                out.append(f"\nDiff since previous snapshot (id={previous[0]}):")
                d_entities = (current[2] or 0) - (previous[2] or 0)
                d_edges = (current[3] or 0) - (previous[3] or 0)
                d_communities = (current[4] or 0) - (previous[4] or 0)
                out.extend(
                    [
                        f"  Entity delta: {d_entities:+d}",
                        f"  Edge delta: {d_edges:+d}",
                        f"  Community delta: {d_communities:+d}",
                    ]
                )
                try:
                    prev_new = _json.loads(previous[7]) if previous[7] else []
                    curr_new = _json.loads(current[7]) if current[7] else []
                    if curr_new:
                        out.append(f"  New entities (this window): {', '.join(curr_new[:5])}")
                    if prev_new:
                        out.append(f"  New entities (previous window): {', '.join(prev_new[:5])}")
                except Exception as e:
                    logger.warning("Unhandled exception in memory_graph_evolution: %s", e)
            else:
                out.append("\nNo previous snapshot to diff against.")

        return "\n".join(out)
    except Exception as e:
        logger.exception("in memory_graph_evolution")
        return _err(ErrorCode.DB_ERROR, f"memory_graph_evolution: {e}")
