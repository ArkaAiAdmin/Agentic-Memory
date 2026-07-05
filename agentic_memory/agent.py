"""Agent-scoped memory class — namespace isolation for multi-agent setups."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List

from agentic_memory.client import MemoryClient
from agentic_memory.models import AgentInfo, MemoryResult, SearchResults
from agentic_memory.utils import resolve_db_path, get_db_connection, safe_close_db
from infra.db_write_queue import sqlite_write_queue

logger = logging.getLogger(__name__)


class AgentMemory:
    """Agent-scoped memory with namespace isolation.

    Each ``AgentMemory`` instance wraps ``MemoryClient`` with automatic
    agent context, scoping all save/search operations to the agent's
    namespace.

    Examples::

        am = AgentMemory(agent_id="coder-1", display_name="Coder Agent")
        am.save("Frontend uses React with TypeScript")
        results = am.search("frontend")
    """

    def __init__(
        self,
        agent_id: str,
        display_name: str = "",
        parent_agent: str | None = None,
        db_path: str | Path | None = None,
    ):
        from agent_context import init_agent

        self._agent_id = agent_id
        self._display_name = display_name or agent_id
        self._parent_agent = parent_agent
        self._db_path = resolve_db_path(db_path)
        self._ctx = init_agent(
            agent_id=agent_id,
            display_name=self._display_name,
            parent_agent=parent_agent,
        )

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def client(self) -> MemoryClient:
        """Return a ``MemoryClient`` instance sharing the same DB."""
        return MemoryClient(db_path=self._db_path)

    @property
    def info(self) -> AgentInfo:
        """Return agent context metadata."""
        return AgentInfo(
            agent_id=self._ctx.agent_id,
            display_name=self._ctx.display_name,
            parent_agent=self._parent_agent or "",
            namespace=self._ctx.namespace,
        )

    # ── Save / Search ──────────────────────────────────────────────────

    def save(
        self,
        content: str,
        category: str = "agents",
        tags: list[str] | None = None,
    ) -> str:
        """Save a memory scoped to this agent.

        Args:
            content: Text content to store.
            category: Memory category (default ``"agents"``).
            tags: Optional list of tag strings.

        Returns:
            The scoped note ID.
        """
        from agent_context import agent_save

        slug = f"auto-{time.strftime('%Y%m%d_%H%M%S')}"
        return str(
            agent_save(
                content=content,
                category=category,
                title_slug=slug,
                tags=tags or [],
            )
        )

    def search(self, query: str, limit: int = 10) -> SearchResults:
        """Search only this agent's memories.

        Args:
            query: Natural-language query string.
            limit: Maximum results to return.

        Returns:
            A ``SearchResults`` container scoped to this agent.
        """
        from agent_context import agent_search

        raw = agent_search(query=query, limit=limit, rerank=True)
        results_list = raw.get("results", [])
        results = [
            MemoryResult(
                id=r.get("id", ""),
                content=r.get("content", ""),
                score=float(r.get("final_score", r.get("rank", 0))),
                tags=r.get("tags", []),
                category=r.get("category", ""),
                created_at=r.get("created_at", ""),
                pinned=bool(r.get("pinned", False)),
                importance=int(r.get("importance", 3)),
            )
            for r in results_list
        ]
        return SearchResults(
            results=results,
            total=len(results),
            query=query,
        )

    # ── List / Clear ───────────────────────────────────────────────────

    def list(self, limit: int = 50) -> List[MemoryResult]:
        """List recent memories scoped to this agent.

        Direct DB query filtered by the agent's namespace prefix.
        """
        from agent_context import get_agent

        ctx = get_agent()
        conn = get_db_connection(self._db_path)
        try:
            if ctx.namespace == "default" or ctx.namespace is None:
                rows = conn.execute(
                    "SELECT m.id, m.content, m.tags, m.category, "
                    "       m.created_at, m.pinned, m.importance "
                    "FROM memories m "
                    "WHERE m.deleted_at IS NULL "
                    "ORDER BY m.created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                prefix = f"agents/{ctx.namespace}/"
                rows = conn.execute(
                    "SELECT m.id, m.content, m.tags, m.category, "
                    "       m.created_at, m.pinned, m.importance "
                    "FROM memories m "
                    "WHERE m.deleted_at IS NULL AND m.source_file LIKE ? "
                    "ORDER BY m.created_at DESC LIMIT ?",
                    (f"{prefix}%", limit),
                ).fetchall()
            return [
                MemoryResult(
                    id=r[0],
                    content=r[1],
                    tags=r[2] or [],
                    category=r[3] or "",
                    created_at=r[4] or "",
                    pinned=bool(r[5]),
                    importance=int(r[6] or 3),
                )
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    def clear(self) -> int:
        """Clear all memories scoped to this agent. Returns count deleted."""
        from agent_context import get_agent

        ctx = get_agent()
        if ctx.namespace == "default" or ctx.namespace is None:
            return 0

        prefix = f"agents/{ctx.namespace}/"
        conn = sqlite_write_queue.start_session(Path(self._db_path))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            n = conn.execute(
                "DELETE FROM memories WHERE source_file LIKE ?",
                (f"{prefix}%",),
            ).rowcount
            conn.commit()
            return int(n) if n is not None else 0
        finally:
            conn.close()

    # ── Static helpers ─────────────────────────────────────────────────

    @staticmethod
    def list_agents() -> List[AgentInfo]:
        """List all registered agent contexts.

        Returns:
            List of ``AgentInfo`` objects for every agent that has been
            initialized in this process.
        """
        from agent_context import list_agents as _list_agents

        agents = _list_agents()
        return [
            AgentInfo(
                agent_id=aid,
                display_name=meta.get("display_name", ""),
                parent_agent=meta.get("parent_agent", ""),
                namespace=meta.get("namespace", ""),
            )
            for aid, meta in agents.items()
        ]

    def reset(self) -> None:
        """Clear the current agent context (revert to default namespace)."""
        from agent_context import clear_agent

        clear_agent()

    # ── Context manager ────────────────────────────────────────────────

    def __enter__(self) -> AgentMemory:
        return self

    def __exit__(self, *args: Any) -> None:
        self.reset()
