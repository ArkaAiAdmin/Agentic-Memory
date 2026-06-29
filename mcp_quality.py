"""
Quality gate MCP tools — memory_quality_filter, memory_quality_stats.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import os


import json
from mcp_common import (
    _resolve_memory_dir,
    get_memory_paths,
    logger,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_instance import mcp
from search_pipeline import search_memories


@mcp.tool()
@with_audit("memory_quality_filter")
def memory_quality_filter(query: str, limit: int = 50) -> str:
    """Search and apply quality gates (validation + deduplication) to results.

    Opt-in via MEMORY_QUALITY_GATES=1. Returns filtered results.
    """
    import quality_gates as qg

    if not qg.QUALITY_GATES_ENABLED:
        return json.dumps(
            {"enabled": False, "message": "Set MEMORY_QUALITY_GATES=1 to enable."}
        )
    try:
        active_dir = _resolve_memory_dir()
        if os.environ.get("MEMORY_DB_PATH"):
            local_db = active_dir / "memory.db"
        else:
            _, local_mem, _ = get_memory_paths()
            local_db = local_mem / "memory.db"
        if not local_db.exists():
            return "No results found."
        raw = search_memories(
            local_db,
            query,
            limit=limit,
            include_global=True,
            rerank=False,
            safety_wiring=False,
        )
        results = raw.get("results", [])
        if not results:
            return "No results found."
        filtered, stats = qg.filter_results(results)
        return json.dumps({"results": filtered, "stats": stats}, indent=2)
    except Exception:
        logger.exception("Quality filter failed")
        return _err(ErrorCode.QUALITY_ERROR, "Quality filter failed")


@mcp.tool()
@with_audit("memory_quality_stats")
def memory_quality_stats() -> str:
    """Return quality gate statistics (note counts, content length distribution)."""
    import quality_gates as qg

    if not qg.QUALITY_GATES_ENABLED:
        return json.dumps({"enabled": False})
    try:
        active_dir = _resolve_memory_dir()
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(ErrorCode.DB_ERROR, f"memory.db not found at {db_path}")
        stats = qg.quality_stats_db(db_path)
        return json.dumps(stats, indent=2)
    except Exception:
        logger.exception("Stats failed")
        return _err(ErrorCode.QUALITY_ERROR, "Stats failed")
