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
import re
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
_default_fallback_emitted = False


from contextlib import contextmanager


@dataclass(frozen=True)
class AgentContext:
    """Immutable agent identity for memory scoping."""

    agent_id: str
    display_name: str = ""
    parent_agent: Optional[str] = None
    namespace: str = ""


@contextmanager
def temporary_agent_context(agent_id: str):
    """Temporarily bind the thread-local agent context to *agent_id*.

    Restores the previous context on exit.  Use this when an
    ``AgentMemory`` instance needs to run operations scoped to its own
    agent identity without permanently overwriting the thread-local
    state (which would break other instances sharing the same thread).
    """
    prev = getattr(_AGENT_CONTEXT, "current", None)
    try:
        if prev and prev.agent_id == agent_id:
            yield prev
            return
        ctx = AgentContext(
            agent_id=agent_id,
            display_name=agent_id,
            namespace=agent_id,
        )
        _AGENT_CONTEXT.current = ctx
        yield ctx
    finally:
        if prev is not None:
            _AGENT_CONTEXT.current = prev
        else:
            try:
                del _AGENT_CONTEXT.current
            except AttributeError:
                pass


def init_agent(
    agent_id: str,
    display_name: str = "",
    parent_agent: Optional[str] = None,
    namespace: str = "",
) -> AgentContext:
    """Register and activate an agent context.

    All subsequent save/search operations will be scoped to this agent
    until ``clear_agent()`` is called or the thread exits.

    Also writes a row to the persistent ``agent_registry_crdt`` table
    for cross-agent discovery via sync.  The DB write is best-effort
    (silently skipped if the table does not yet exist, e.g. during
    early bootstrap before migrations run).

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

    # Best-effort persistent write for cross-agent discovery via sync.
    _persist_agent_registration(agent_id, ctx.display_name, parent_agent, ctx.namespace)

    logger.info("agent_context: activated %s (namespace=%s)", agent_id, ctx.namespace)
    return ctx


def _persist_agent_registration(
    agent_id: str,
    display_name: str,
    parent_agent: Optional[str],
    namespace: str,
) -> None:
    """Write an agent identity row to ``agent_registry_crdt``.

    Best-effort: silently skips if the table doesn't exist yet
    (pre-migration bootstrap) or the DB path isn't available.
    """
    _now = __import__("time").time()
    try:
        from pathlib import Path as _Path
        import sqlite3

        from infra._lazy_imports import get_config as _get_config

        cfg = _get_config()
        db = _Path(str(cfg.db_path))
        if not db.exists():
            return
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO agent_registry_crdt
                   (agent_id, display_name, parent_agent, namespace,
                    logical_clock, version_vector, last_seen, is_deleted, tenant_id)
                   VALUES (?, ?, ?, ?, 1, '{}', ?, 0, 'default')""",
                (agent_id, display_name, parent_agent or "", namespace, _now),
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet (pre-migration)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass


def get_agent() -> AgentContext:
    """Return the current agent context, or a default singleton.

    In MCP server mode (FastMCP), tool calls run in a thread pool where
    each thread has its own thread-local storage.  On first access per
    thread, we read ``MEMORY_AGENT_ID`` from the process environment and
    cache the result for that thread.  This ensures every tool call
    resolves the correct agent identity without requiring an explicit
    ``init_agent()`` call in every thread.
    """
    try:
        val = getattr(_AGENT_CONTEXT, "current", None)
        if isinstance(val, AgentContext):
            return val
        raise AttributeError
    except AttributeError:
        env_agent = os.environ.get("MEMORY_AGENT_ID", "").strip()
        if env_agent:
            ctx = AgentContext(agent_id=env_agent, namespace=env_agent)
        else:
            global _default_fallback_emitted
            if not _default_fallback_emitted:
                logger.warning(
                    "agent_context: no agent set in this thread and "
                    "MEMORY_AGENT_ID not set. Falling back to 'default'."
                )
                _default_fallback_emitted = True
            ctx = AgentContext(agent_id="default", namespace="default")
        _AGENT_CONTEXT.current = ctx
        _AGENT_REGISTRY[ctx.agent_id] = {
            "display_name": ctx.agent_id,
            "parent_agent": None,
            "namespace": ctx.namespace,
            "created_at": __import__("time").time(),
        }
        return ctx


def clear_agent() -> None:
    """Clear the current thread's agent context (reverts to default)."""
    try:
        del _AGENT_CONTEXT.current
    except AttributeError:
        pass


def list_agents() -> dict[str, dict]:
    """Return all registered agent contexts.

    Merges in-memory (current process) agents with persisted agent
    registry entries from the DB for cross-agent discovery.
    """
    result = {}
    with _AGENT_LOCK:
        result.update(dict(_AGENT_REGISTRY))

    # Supplement with persisted entries from agent_registry_crdt
    try:
        from pathlib import Path as _Path
        import sqlite3

        from infra._lazy_imports import get_config as _get_config

        cfg = _get_config()
        db = _Path(str(cfg.db_path))
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=5)
            try:
                rows = conn.execute(
                    """SELECT agent_id, display_name, parent_agent, namespace, last_seen
                       FROM agent_registry_crdt
                       WHERE is_deleted = 0 AND tenant_id = 'default'""",
                ).fetchall()
                for row in rows:
                    aid = row[0]
                    if aid not in result:
                        result[aid] = {
                            "display_name": row[1],
                            "parent_agent": row[2] or None,
                            "namespace": row[3],
                            "last_seen": row[4],
                        }
            except sqlite3.OperationalError:
                pass  # table doesn't exist yet
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception:
        pass

    return result


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

    Namespace is validated against ``[A-Za-z0-9._-]+`` to prevent SQL injection
    via f-string interpolation into the LIKE clause.
    """
    ctx = get_agent()
    if ctx.namespace == "default":
        return "1=1"
    ns = ctx.namespace
    if not re.fullmatch(r"[A-Za-z0-9._-]+", ns):
        raise ValueError(f"Invalid namespace: {ns!r} (expected [A-Za-z0-9._-]+)")
    prefix = f"agents/{ns}/"
    return f"{column} LIKE '{prefix}%'"


# ---------------------------------------------------------------------------
# Minimal MCP-compatible save / search wrappers
# ---------------------------------------------------------------------------


def agent_save(content: str, category: str, title_slug: str, **kwargs):
    """Save a memory scoped to the current agent.

    Wraps ``save_pipeline.save_memory`` with automatic note ID scoping.
    """
    from infra._lazy_imports import save_memory_auto, SaveRequest

    scoped_slug = scope_note_id(title_slug)
    _now_iso = kwargs.pop("_now_iso", None)
    _conn = kwargs.pop("_conn", None)
    kwargs.pop("metadata_json", None)
    return save_memory_auto(
        SaveRequest(
            content=content,
            category=category,
            title_slug=scoped_slug,
            **kwargs,
        ),
        _now_iso=_now_iso,
        _conn=_conn,
    )


def agent_search(query: str, limit: int = 5, rerank: bool = True, include_global: bool = True) -> dict:
    """Search memories scoped to the current agent.

    Wraps ``search_pipeline.search_memories`` with automatic agent
    namespace filtering. Returns the same dict shape as search_memories.

    Args:
        query: Search query string.
        limit: Max results to return.
        rerank: Whether to apply cross-encoder reranking.
        include_global: If True, include global (cross-agent) memories in
            results alongside agent-scoped ones. Defaults to True.
    """
    from infra._lazy_imports import search_memories, get_config
    from pathlib import Path

    ctx = get_agent()
    # Over-fetch when post-filtering by namespace to avoid starving results.
    # The post-filter may remove non-matching entries, leaving fewer than
    # the requested limit.
    fetch_limit = limit * 4 if ctx.namespace != "default" else limit

    db_path = Path(get_config().db_path)
    result = search_memories(
        db_path=db_path,
        query=query,
        limit=fetch_limit,
        include_global=include_global,
        rerank=rerank,
    )
    # Post-filter by agent namespace if not in default
    if ctx.namespace != "default":
        prefix = f"agents/{ctx.namespace}/"
        if isinstance(result, dict) and "results" in result:
            filtered = [
                r for r in result["results"] if r.get("id", "").startswith(prefix)
            ]
            result["results"] = filtered[:limit]
            result["total"] = len(filtered)
    return cast(dict, result)
