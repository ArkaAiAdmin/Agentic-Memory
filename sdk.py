#!/usr/bin/env python3
"""Agentic Memory SDK — Drop-in replacement for Mem0's 3-line API.

Quick start::

    from agentic_memory import Memory

    m = Memory()  # or Memory(db_path="/custom/path/memory.db")
    m.add("User prefers dark mode")
    results = m.search("What does the user prefer?")

    # With agent scoping:
    from agentic_memory import AgentMemory

    am = AgentMemory(agent_id="coder-1")
    am.save("This is important for the frontend module")
    results = am.search("frontend")

Also provides:
    - Memory(user_id="...") → per-user memory isolation
    - Memory.search(query, limit=10, rerank=True)
    - Memory.delete(note_id)
    - Memory.list(limit=50)
    - Memory.clear() — clears all memory for the current user/agent
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Memory:
    """Minimal memory store — Mem0-compatible API.

    Initializes from the default config path or an explicit db_path.

    Examples::

        m = Memory()
        m.add("User prefers dark mode")
        m.add("User is learning Rust", tags=["rust", "learning"])
        results = m.search("What languages does the user know?")
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        user_id: str = "default",
        config: Optional[dict] = None,
    ):
        if db_path:
            self._db_path = Path(db_path)
        else:
            from infra._lazy_imports import get_config

            self._db_path = Path(get_config().db_path)
        self._user_id = user_id
        self._config = config or {}

    def add(self, content: str, tags: Optional[list[str]] = None) -> str:
        """Add a memory and return its note ID.

        Compatible with Mem0's ``m.add("text")`` API.
        """
        from infra._lazy_imports import save_memory

        ts = time.strftime("%Y%m%d_%H%M%S")
        title_slug = f"sdk-auto-{ts}-{hash(content) & 0xFFFF:04x}"
        note_id = save_memory(
            content=content,
            category="sdk",
            title_slug=title_slug,
            tags=tags or [],
            pinned=False,
            is_global=True,
        )
        return str(note_id)

    def search(self, query: str, limit: int = 10, rerank: bool = True) -> list[dict]:
        """Search memories by semantic relevance.

        Compatible with Mem0's ``m.search("query")`` API.

        Returns a list of dicts with keys: id, content, score, tags.
        """
        from infra._lazy_imports import search_memories

        result = search_memories(
            db_path=self._db_path,
            query=query,
            limit=limit,
            include_global=True,
            rerank=rerank,
        )
        if isinstance(result, str):
            result = json.loads(result)
        if isinstance(result, dict):
            rs = result.get("results", [])
            return [
                {
                    "id": r.get("id", ""),
                    "content": r.get("content", ""),
                    "score": r.get("final_score", r.get("rank", 0)),
                    "tags": r.get("tags", []),
                }
                for r in rs
            ]
        return list(result) if isinstance(result, list) else []

    def delete(self, note_id: str) -> bool:
        """Soft-delete a memory by note ID.

        Returns True if the note was found and deleted.
        """
        from memory_delete import soft_delete_note

        return soft_delete_note(str(self._db_path), note_id)

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List recent memories."""
        from infra._lazy_imports import connection_pool, safe_close_db

        conn = connection_pool.get(str(self._db_path), timeout=10.0)
        try:
            rows = conn.execute(
                "SELECT id, content, tags, created_at FROM memories "
                "WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [
                {"id": r[0], "content": r[1], "tags": r[2], "created_at": r[3]}
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    def clear(self) -> int:
        """Clear all SDK-created memories. Returns count cleared.

        P1-11 fix: route through the connection pool instead of opening a
        raw sqlite3.connect() + bare conn.close(). The previous version
        also did an uncascaded DELETE, leaving orphan rows in
        memory_embeddings, memory_chunks, memory_vec_keys, user_access_log,
        and kg_facts. We now rely on the FK trigger to cascade, and we
        use the pool so the connection is properly returned.
        """
        from infra.db_write_queue import sqlite_write_queue

        conn = sqlite_write_queue.start_session(Path(self._db_path))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            n = conn.execute(
                "DELETE FROM memories WHERE source_file LIKE 'sdk-%'"
            ).rowcount
            conn.commit()
            return int(n) if n is not None else 0
        finally:
            conn.close()

    def stats(self) -> dict:
        """Return memory and vector index stats."""
        from infra._lazy_imports import connection_pool, safe_close_db

        conn = connection_pool.get(str(self._db_path), timeout=10.0)
        try:
            return {
                "memories": conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
                ).fetchone()[0],
                "vector_keys": conn.execute(
                    "SELECT COUNT(*) FROM memory_vec_keys"
                ).fetchone()[0],
                "chunks": conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[
                    0
                ],
            }
        finally:
            safe_close_db(conn)


class AgentMemory:
    """Agent-scoped memory with namespace isolation.

    Wraps ``Memory`` with automatic agent context. Each agent's memories
    are isolated via the ``agent_context`` module.

    Examples::

        am = AgentMemory(agent_id="coder-1")
        am.save("Frontend uses React with TypeScript")
        results = am.search("What does the frontend use?")
    """

    def __init__(
        self,
        agent_id: str,
        display_name: str = "",
        parent_agent: Optional[str] = None,
        db_path: Optional[str | Path] = None,
    ):
        from agent_context import init_agent

        self._ctx = init_agent(
            agent_id=agent_id,
            display_name=display_name or agent_id,
            parent_agent=parent_agent,
        )
        self._mem = Memory(db_path=db_path)

    def save(self, content: str, tags: Optional[list[str]] = None) -> str:
        """Save a memory scoped to this agent."""
        from agent_context import agent_save

        return str(agent_save(
            content=content,
            category="agents",
            title_slug=f"auto-{time.strftime('%Y%m%d_%H%M%S')}",
            tags=tags or [],
        ))

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search only this agent's memories."""
        from agent_context import agent_search

        result = agent_search(query=query, limit=limit)
        return [
            {
                "id": r.get("id", ""),
                "content": r.get("content", ""),
                "score": r.get("final_score", 0),
            }
            for r in result.get("results", [])
        ]
