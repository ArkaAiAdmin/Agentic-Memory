"""
User profile MCP tools — memory_profile_access, memory_user_profile, memory_profile_stats.
"""



import json
from mcp_common import logger, _err, ErrorCode, with_audit
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_profile_access")
def memory_profile_access(
    note_id: str, source: str = "search", category: str = "", tags: str = ""
) -> str:
    """Record that a note was accessed. Opt-in via MEMORY_USER_PROFILE=1."""
    import user_profile as up

    if not up.PROFILE_ENABLED:
        return json.dumps(
            {"enabled": False, "message": "Set MEMORY_USER_PROFILE=1 to enable."}
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        ok = up.record_access(
            note_id, source=source, category=category or None, tags=tag_list or None
        )
        return json.dumps({"recorded": ok, "note_id": note_id})
    except Exception:
        logger.exception("Access recording failed")
        return _err(ErrorCode.PROFILE_ERROR, "Access recording failed")


@mcp.tool()
@with_audit("memory_user_profile")
def memory_user_profile() -> str:
    """Get the user preference profile from access history.

    Opt-in via MEMORY_USER_PROFILE=1.
    """
    import user_profile as up

    if not up.PROFILE_ENABLED:
        return json.dumps({"enabled": False})
    try:
        profile = up.get_user_profile()
        return json.dumps(profile, indent=2, default=str)
    except Exception:
        logger.exception("Profile failed")
        return _err(ErrorCode.PROFILE_ERROR, "Profile failed")


@mcp.tool()
@with_audit("memory_profile_stats")
def memory_profile_stats() -> str:
    """Return user profiling statistics."""
    import user_profile as up

    if not up.PROFILE_ENABLED:
        return json.dumps({"enabled": False})
    try:
        return json.dumps(up.profile_stats(), indent=2)
    except Exception:
        logger.exception("Stats failed")
        return _err(ErrorCode.PROFILE_ERROR, "Stats failed")
