#!/usr/bin/env python3
"""Agent context manager — per-agent memory scoping with namespace isolation.

Provides:
- agent_context.create("agent-name") → isolated namespace
- agent_context.get() → current agent ID
- scope_note_id(note_id) → namespaced note ID
- agent_save / agent_search — scoped save/search with isolation guarantees

For CRDT-based collaborative editing across agents, see ``crdt_merge.py``.
For temporal contradiction resolution, see ``temporal_resolver.py``.

Designed to work alongside the existing memory system without breaking
single-agent code. When no agent is configured, all operations work as
before (default = "default" agent namespace).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional, cast
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thread-local current agent context
# ---------------------------------------------------------------------------

_AGENT_CONTEXT: threading.local = threading.local()
_AGENT_LOCK = threading.Lock()
_AGENT_REGISTRY: dict[str, dict] = {}  # agent_id -> metadata


@dataclass(frozen=True)
class AgentContext:
    """Immutable agent identity for memory scoping."""

    agent_id: str
    display_name: str = ""
    parent_agent: Optional[str] = None
    namespace: str = ""


def init_agent(
    agent_id: str,
    display_name: str = "",
    parent_agent: Optional[str] = None,
    namespace: str = "",
) -> AgentContext:
    """Register and activate an agent context.

    All subsequent save/search operations will be scoped to this agent
    until ``clear_agent()`` is called or the thread exits.

    Args:
        agent_id: Globally unique agent identifier.
        display_name: Human-readable name (optional).
        parent_agent: ID of the agent that spawned this one (optional).
        namespace: Override the default namespace derived from agent_id.
    """
    ctx = AgentContext(
        agent_id=agent_id,
        display_name=display_name or agent_id,
        parent_agent=parent_agent,
        namespace=namespace or agent_id,
    )
    with _AGENT_LOCK:
        _AGENT_REGISTRY[agent_id] = {
            "display_name": ctx.display_name,
            "parent_agent": parent_agent,
            "namespace": ctx.namespace,
            "created_at": __import__("time").time(),
        }
    _AGENT_CONTEXT.current = ctx
    logger.info("agent_context: activated %s (namespace=%s)", agent_id, ctx.namespace)
    return ctx


def get_agent() -> AgentContext:
    """Return the current agent context, or a default singleton."""
    try:
        val = getattr(_AGENT_CONTEXT, "current", None)
        if isinstance(val, AgentContext):
            return val
        raise AttributeError
    except AttributeError:
        env_agent = os.environ.get("MEMORY_AGENT_ID")
        if env_agent and env_agent.strip():
            ctx = AgentContext(agent_id=env_agent, namespace=env_agent)
        else:
            ctx = AgentContext(agent_id="default", namespace="default")
        _AGENT_CONTEXT.current = ctx
        return ctx


def clear_agent() -> None:
    """Clear the current thread's agent context (reverts to default)."""
    try:
        del _AGENT_CONTEXT.current
    except AttributeError:
        pass


def list_agents() -> dict[str, dict]:
    """Return all registered agent contexts."""
    with _AGENT_LOCK:
        return dict(_AGENT_REGISTRY)


# ---------------------------------------------------------------------------
# Note ID scoping helpers
# ---------------------------------------------------------------------------


def scope_note_id(note_id: str) -> str:
    """Prefix a note ID with the current agent namespace.

    ``"lessons/foo"`` (default agent) stays ``"lessons/foo"``.
    ``"lessons/foo"`` (agent ``"coder-1"``) becomes ``"agents/coder-1/lessons/foo"``.

    Keeps note IDs unique across agents without collisions.
    """
    ctx = get_agent()
    if ctx.namespace == "default" or ctx.namespace is None:
        return note_id
    if note_id.startswith("agents/"):
        return note_id  # already scoped
    return f"agents/{ctx.namespace}/{note_id}"


def unscope_note_id(note_id: str) -> str:
    """Strip the agent namespace prefix from a note ID.

    ``"agents/coder-1/lessons/foo"`` → ``"lessons/foo"``.
    Returns the original if not scoped.
    """
    if note_id.startswith("agents/") and note_id.count("/") >= 2:
        return note_id.split("/", 2)[2]
    return note_id


def agent_filter_clause(column: str = "m.source_file") -> str:
    """Return a SQL WHERE fragment to filter by the current agent namespace.

    When the default agent is active, returns ``"1=1"`` (no filter).
    For scoped agents, returns a LIKE condition against the note ID prefix.
    """
    ctx = get_agent()
    if ctx.namespace == "default":
        return "1=1"
    prefix = f"agents/{ctx.namespace}/"
    return f"{column} LIKE '{prefix}%'"


# ---------------------------------------------------------------------------
# Minimal MCP-compatible save / search wrappers
# ---------------------------------------------------------------------------


def agent_save(content: str, category: str, title_slug: str, **kwargs):
    """Save a memory scoped to the current agent.

    Wraps ``save_pipeline.save_memory`` with automatic note ID scoping.
    """
    from _lazy_imports import save_memory

    scoped_slug = scope_note_id(title_slug)
    # Remove metadata_json if present (save_memory doesn't accept it)
    kwargs.pop("metadata_json", None)
    return save_memory(
        content=content,
        category=category,
        title_slug=scoped_slug,
        **kwargs,
    )


def agent_search(query: str, limit: int = 5, rerank: bool = True) -> dict:
    """Search memories scoped to the current agent.

    Wraps ``search_pipeline.search_memories`` with automatic agent
    namespace filtering. Returns the same dict shape as search_memories.
    """
    from _lazy_imports import search_memories, get_config
    from pathlib import Path

    db_path = Path(get_config().db_path)
    result = search_memories(
        db_path=db_path,
        query=query,
        limit=limit,
        include_global=True,
        rerank=rerank,
    )
    # Post-filter by agent namespace if not in default
    ctx = get_agent()
    if ctx.namespace != "default":
        prefix = f"agents/{ctx.namespace}/"
        if isinstance(result, dict) and "results" in result:
            filtered = [
                r for r in result["results"] if r.get("id", "").startswith(prefix)
            ]
            result["results"] = filtered
            result["total"] = len(filtered)
    return cast(dict, result)
