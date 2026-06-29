"""
Multi-agent sharing MCP tools — memory_share, memory_shared_list,
memory_shared_import, memory_shared_stats, memory_auto_share,
memory_share_candidates.
"""
from mcp_common import _bootstrap_path  # noqa: E402



import json
from mcp_common import _err, ErrorCode, logger, with_audit
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_share")
def memory_share(note_id: str, agent_id: str) -> str:
    """Share a memory to the cross-agent shared pool.

    Opt-in via MEMORY_MULTI_AGENT=1.
    """
    import memory_sharing as ma

    if not ma.MULTI_AGENT_ENABLED:
        return json.dumps(
            {"enabled": False, "message": "Set MEMORY_MULTI_AGENT=1 to enable."}
        )
    try:
        result = ma.share_memory(note_id, agent_id)
        return json.dumps(result, indent=2)
    except Exception:
        logger.exception("Share failed")
        return _err(ErrorCode.SHARE_ERROR, "Share failed")


@mcp.tool()
@with_audit("memory_shared_list")
def memory_shared_list(agent_id: str = "", category: str = "", limit: int = 50) -> str:
    """List memories in the shared pool. Opt-in via MEMORY_MULTI_AGENT=1."""
    import memory_sharing as ma

    if not ma.MULTI_AGENT_ENABLED:
        return json.dumps({"enabled": False})
    try:
        results = ma.list_shared_memories(
            agent_id=agent_id or None,
            category=category or None,
            limit=limit,
        )
        return json.dumps(results, indent=2, default=str)
    except Exception:
        logger.exception("List shared failed")
        return _err(ErrorCode.SHARE_ERROR, "List shared failed")


@mcp.tool()
@with_audit("memory_shared_import")
def memory_shared_import(shared_id: str, target_agent_id: str) -> str:
    """Import a shared memory into the target agent's workspace.

    Opt-in via MEMORY_MULTI_AGENT=1.
    """
    import memory_sharing as ma

    if not ma.MULTI_AGENT_ENABLED:
        return json.dumps({"enabled": False})
    try:
        result = ma.import_shared_memory(shared_id, target_agent_id)
        return json.dumps(result, indent=2)
    except Exception:
        logger.exception("Import shared failed")
        return _err(ErrorCode.SHARE_ERROR, "Import shared failed")


@mcp.tool()
@with_audit("memory_shared_stats")
def memory_shared_stats() -> str:
    """Return shared pool statistics."""
    import memory_sharing as ma

    if not ma.MULTI_AGENT_ENABLED:
        return json.dumps({"enabled": False})
    try:
        return json.dumps(ma.shared_pool_stats(), indent=2)
    except Exception:
        logger.exception("Shared stats failed")
        return _err(ErrorCode.SHARE_ERROR, "Shared stats failed")


@mcp.tool()
@with_audit("memory_auto_share")
def memory_auto_share(
    agent_id: str = "",
    min_importance: int = 0,
    min_fitness: float = 0.0,
    limit: int = 0,
    dry_run: bool = False,
) -> str:
    """Scan high-importance memories and offer to share them with peers.

    Auto-share is the P2 #1 wire-up that gives the ``shared_memories``
    table real content: it identifies "share-worthy" notes (high
    importance + high fitness, not already in the pool) and copies
    them in. Default thresholds are importance>=4 and fitness>=0.6.

    Args:
        agent_id:       sharing agent (defaults to local CRDT agent id).
        min_importance: override the importance threshold (1-5).
        min_fitness:    override the fitness threshold (0.0-1.0).
        limit:          max notes to share in this call (default 25).
        dry_run:        if True, list candidates but do not share.

    Returns JSON with ``scanned``, ``shared``, ``skipped``,
    ``candidates``, ``shared_ids``.
    """
    import memory_sharing as ma

    if not ma.MULTI_AGENT_ENABLED:
        return json.dumps(
            {"enabled": False, "message": "Set MEMORY_MULTI_AGENT=1 to enable."}
        )
    try:
        if dry_run:
            kwargs = {
                "min_importance": min_importance or ma._AUTO_SHARE_MIN_IMPORTANCE,
                "min_fitness": min_fitness or ma._AUTO_SHARE_MIN_FITNESS,
                "limit": limit or ma._AUTO_SHARE_MAX_PER_CYCLE,
            }
            candidates = ma.list_share_candidates(**kwargs)
            return json.dumps(
                {
                    "enabled": True,
                    "dry_run": True,
                    "candidates": candidates,
                    "count": len(candidates) if isinstance(candidates, list) else 0,
                },
                indent=2,
                default=str,
            )
        kwargs = {
            "min_importance": min_importance or ma._AUTO_SHARE_MIN_IMPORTANCE,
            "min_fitness": min_fitness or ma._AUTO_SHARE_MIN_FITNESS,
            "limit": limit or ma._AUTO_SHARE_MAX_PER_CYCLE,
        }
        if agent_id:
            kwargs["agent_id"] = agent_id
        result = ma.auto_share_high_value(**kwargs)
        return json.dumps(result, indent=2, default=str)
    except Exception:
        logger.exception("Auto-share failed")
        return _err(ErrorCode.SHARE_ERROR, "Auto-share failed")
