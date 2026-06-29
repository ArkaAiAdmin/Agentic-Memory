"""MCP tools for agent context management (per-agent memory scoping).

Provides ADMIN tools for initializing, clearing, and listing agent contexts.
Agent scoping enables namespace isolation for multi-agent CRDT systems.
"""
from mcp_common import _bootstrap_path  # noqa: E402

import json

from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit


@mcp.tool()
@with_audit("memory_agent_init")
def memory_agent_init(
    agent_id: str,
    display_name: str = "",
    parent_agent: str = "",
    namespace: str = "",
) -> str:
    """Initialize a new agent context for memory scoping.

    All subsequent save/search operations will be scoped to this agent
    until ``memory_agent_clear`` is called. Use ``memory_agent_list``
    to see active agents.

    Args:
        agent_id: Globally unique agent identifier.
        display_name: Human-readable name (optional).
        parent_agent: ID of the agent that spawned this one (optional).
        namespace: Override default namespace (defaults to agent_id).
    """
    try:
        from _lazy_imports import init_agent

        ctx = init_agent(
            agent_id=agent_id,
            display_name=display_name or agent_id,
            parent_agent=parent_agent or None,
            namespace=namespace or agent_id,
        )
        return json.dumps(
            {
                "ok": True,
                "agent_id": ctx.agent_id,
                "namespace": ctx.namespace,
                "display_name": ctx.display_name,
            }
        )
    except Exception as e:
        return _err(ErrorCode.PROFILE_ERROR, str(e))


@mcp.tool()
@with_audit("memory_agent_clear")
def memory_agent_clear() -> str:
    """Clear the current agent context (revert to default namespace).

    After clearing, subsequent save/search operations operate in the
    default (unscoped) namespace.
    """
    try:
        from _lazy_imports import clear_agent

        clear_agent()
        return json.dumps({"ok": True, "message": "Agent context cleared"})
    except Exception as e:
        return _err(ErrorCode.PROFILE_ERROR, str(e))


@mcp.tool()
@with_audit("memory_agent_list")
def memory_agent_list() -> str:
    """List all registered agent contexts.

    Returns:
        Dict with agents key mapping agent_id to metadata.
    """
    try:
        from _lazy_imports import list_agents

        agents = list_agents()
        return json.dumps({"ok": True, "agents": agents})
    except Exception as e:
        return _err(ErrorCode.PROFILE_ERROR, str(e))
