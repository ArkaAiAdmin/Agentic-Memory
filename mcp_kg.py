"""
Knowledge Graph MCP tools — graph_search, graph_stats, facts_search, facts_list, facts_stats.
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


@mcp.tool()
@with_audit("memory_graph_search")
def memory_graph_search(query: str, limit: int = 10, max_hops: int = 2) -> str:
    """Search the knowledge graph for entities matching the query.

    Returns entities with their relations, ranked by mention count.
    Requires MEMORY_KNOWLEDGE_GRAPH=1 to be enabled.
    """
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
    try:
        from fact_extraction import facts_search_db

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
    try:
        from fact_extraction import facts_list_db

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
    try:
        from fact_extraction import facts_stats_db

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
