"""
Knowledge Graph Traversal MCP tools — memory_graph_shortest_path, memory_graph_traverse.
"""

from typing import List, Union

from mcp_surface.mcp_common import (
    _resolve_memory_dir,
    logger,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_surface.mcp_instance import mcp


@mcp.tool()
@with_audit("memory_graph_shortest_path")
def memory_graph_shortest_path(source: str, target: str, max_depth: int = 5) -> str:
    """Compute the shortest path of relations and entities between two nodes in the Knowledge Graph.

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
        max_depth = max(1, min(int(max_depth), 10))
    except (ValueError, TypeError):
        max_depth = 5

    try:
        from infra.db import open_db
        from kg.kg_traversal import find_shortest_path

        with open_db(db_path, timeout=5.0, write=False) as conn:
            path = find_shortest_path(conn, source, target, max_depth=max_depth)

        if not path:
            return f"No path found between '{source}' and '{target}' within depth {max_depth}."

        lines = [f"**Shortest Path from '{source}' to '{target}' (length={len(path)//2}):**"]
        path_str_parts = []
        for element in path:
            if "relation" in element:
                path_str_parts.append(f"--[{element['relation']}]-->")
            else:
                path_str_parts.append(f"[{element['entity_type']}] {element['name']}")

        lines.append("  " + " ".join(path_str_parts))
        return "\n".join(lines)

    except Exception as e:
        logger.exception("in memory_graph_shortest_path")
        return _err(ErrorCode.DB_ERROR, f"Error traversing graph: {e}")


@mcp.tool()
@with_audit("memory_graph_traverse")
def memory_graph_traverse(start: str, edge_patterns: Union[str, List[str]]) -> str:
    """Crawl the Knowledge Graph starting from a node, following a sequence of relation types.

    edge_patterns: Comma-separated string or list of relation type strings (e.g. "defines,imports").
    Requires MEMORY_KNOWLEDGE_GRAPH=1.
    """
    from knowledge_graph import KG_ENABLED

    if not KG_ENABLED:
        return "Knowledge graph disabled. Set MEMORY_KNOWLEDGE_GRAPH=1 to enable."

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"no memory.db at {db_path}")

    # Parse edge patterns
    if isinstance(edge_patterns, str):
        if len(edge_patterns) > 500:
            return _err(ErrorCode.INVALID_PARAMS, "edge_patterns string exceeds maximum length of 500 characters")
        patterns = [p.strip() for p in edge_patterns.split(",") if p.strip()]
    elif isinstance(edge_patterns, list):
        if len(edge_patterns) > 50:
            return _err(ErrorCode.INVALID_PARAMS, "edge_patterns list exceeds maximum of 50 entries")
        patterns = [str(p).strip() for p in edge_patterns if str(p).strip()]
    else:
        return _err(ErrorCode.INVALID_PARAMS, "edge_patterns must be a list of strings or a comma-separated string")

    if not patterns:
        return _err(ErrorCode.INVALID_PARAMS, "edge_patterns cannot be empty")

    patterns = patterns[:10]  # Bound traversal sequence depth to 10 steps

    try:
        from infra.db import open_db
        from kg.kg_traversal import traverse_graph

        with open_db(db_path, timeout=5.0, write=False) as conn:
            paths = traverse_graph(conn, start, patterns)

        if not paths:
            rel_path_str = " -> ".join(patterns)
            return f"No paths found matching pattern '{start} -[{rel_path_str}]-> ...'"

        total_paths = len(paths)
        truncated = False
        if total_paths > 100:
            paths = paths[:100]
            truncated = True

        lines = [f"**Traversed {total_paths} paths starting at '{start}' matching relation sequence {patterns}:**"]
        for idx, path in enumerate(paths):
            path_str_parts = []
            for element in path:
                if "relation" in element:
                    path_str_parts.append(f"--[{element['relation']}]-->")
                else:
                    path_str_parts.append(f"{element['name']}")
            lines.append(f"  {idx + 1}. " + " ".join(path_str_parts))

        if truncated:
            lines.append(f"  ... [Truncated: showing first 100 of {total_paths} paths]")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("in memory_graph_traverse")
        return _err(ErrorCode.DB_ERROR, f"Error traversing graph: {e}")
