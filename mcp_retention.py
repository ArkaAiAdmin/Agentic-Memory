"""
Adaptive retention MCP tools — memory_adaptive_retention, memory_retention_stats.

Also runs the surprise-based neural forget curve to decay scores.
"""



import json
from mcp_common import logger, _err, ErrorCode, with_audit
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_adaptive_retention")
def memory_adaptive_retention(dry_run: bool = False) -> str:
    """Batch compute adaptive half-lives and neural forget curve scores."""
    from background.retention_coordinator import run_retention_pipeline
    from infra.infrastructure import resolve_active_memory_dir
    import adaptive_retention as ar

    db_path = resolve_active_memory_dir() / "memory.db"
    if dry_run:
        results = {}
        if ar.ADAPTIVE_RETENTION_ENABLED:
            try:
                results["adaptive_retention"] = ar.batch_update_retention(dry_run=True)
            except Exception as e:
                logger.warning("memory_adaptive_retention failed: %s", e)
                results["adaptive_retention"] = {"error": str(e)}
        return json.dumps(results, indent=2)

    try:
        results = run_retention_pipeline(db_path)
    except Exception as e:
        logger.warning("memory_adaptive_retention failed: %s", e)
        results = {"error": str(e)}

    return json.dumps(
        results if results else {"message": "no retention systems active"}, indent=2
    )


@mcp.tool()
@with_audit("memory_retention_stats")
def memory_retention_stats() -> str:
    """Return adaptive retention statistics."""
    import adaptive_retention as ar

    if not ar.ADAPTIVE_RETENTION_ENABLED:
        return json.dumps({"enabled": False})
    try:
        return json.dumps(ar.retention_stats(), indent=2)
    except Exception:
        logger.exception("Stats failed")
        return _err(ErrorCode.RETENTION_ERROR, "Stats failed")
