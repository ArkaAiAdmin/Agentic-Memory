"""
Adaptive retention MCP tools — memory_adaptive_retention, memory_retention_stats.

Also runs the surprise-based neural forget curve to decay scores.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401



import json
from mcp_common import logger, _err, ErrorCode, with_audit
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_adaptive_retention")
def memory_adaptive_retention(dry_run: bool = False) -> str:
    """Batch compute adaptive half-lives and neural forget curve scores."""
    import adaptive_retention as ar
    import neural_forget as nf
    from infrastructure import resolve_active_memory_dir

    results = {}
    if ar.ADAPTIVE_RETENTION_ENABLED:
        try:
            r = ar.batch_update_retention(dry_run=dry_run)
            results["adaptive_retention"] = r
        except Exception as e:
            results["adaptive_retention"] = {"error": str(e)}

    if not dry_run:
        try:
            db_path = resolve_active_memory_dir() / "memory.db"
            if db_path.exists():
                r = nf.batch_update_retention(db_path)
                results["neural_forget"] = r
        except Exception as e:
            results["neural_forget"] = {"error": str(e)}

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
