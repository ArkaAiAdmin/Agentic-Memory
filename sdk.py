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

Changelog:
  1.0.0 (2026-07-08) — Stable API. Nested config refactor complete.
                        AgentMemory.search gains include_global parameter.
  0.9.0 (2026-06-15) — Bump potion-8M embedding, add AgentMemory class.
  0.8.0 (2026-05-20) — Initial public SDK.
"""

from __future__ import annotations

__version__ = "1.2.0"
__all__ = ["Memory", "AgentMemory"]

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 55) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower())[:max_len].strip("-")


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
        principal_id: str | None = None,
    ):
        if db_path:
            self._db_path = Path(db_path)
        else:
            from infra._lazy_imports import get_config

            self._db_path = Path(get_config().db_path)
        self._user_id = user_id
        self._config = config or {}
        self._principal_id = principal_id

    def add(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        is_global: bool = False,
    ) -> str:
        """Add a memory and return its note ID.

        Compatible with Mem0's ``m.add("text")`` API.

        Args:
            content: The memory text.
            tags: Optional list of tags.
            is_global: If True, saves to the global (cross-project) memory
                scope visible to all workspaces. Defaults to False so SDK
                saves stay scoped to the current project unless explicitly
                opted in.
        """
        from infra._lazy_imports import save_memory_auto
        if self._principal_id:
            from agent_context import _AGENT_CONTEXT
            _AGENT_CONTEXT.principal_id = self._principal_id

        ts = time.strftime("%Y%m%d_%H%M%S")
        raw = content.strip().split("\n")[0][:55]
        slug = _slugify(raw) or f"note-{ts}"
        suffix = hash(content) & 0xFFFF
        title_slug = f"{slug}-{suffix:04x}"
        return str(save_memory_auto(
            content=content,
            category="sdk",
            title_slug=title_slug,
            tags=tags or [],
            pinned=False,
            is_global=is_global,
        ))

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
        if self._principal_id:
            from agent_context import _AGENT_CONTEXT
            _AGENT_CONTEXT.principal_id = self._principal_id

        return soft_delete_note(str(self._db_path), note_id)

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List recent memories."""
        from infra._lazy_imports import connection_pool, safe_close_db

        conn = connection_pool.get(str(self._db_path), timeout=10.0)
        try:
            rows = conn.execute(
                "SELECT id, content, tags, created_at FROM tenant_memories "
                "WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [
                {"id": r[0], "content": r[1], "tags": r[2], "created_at": r[3]}
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    def clear(self, confirm: bool = False, dry_run: bool = False) -> int:
        """Clear all SDK-created memories. Returns count cleared.

        Destructive operation: by default this is a no-op unless
        ``confirm=True`` is explicitly passed, protecting against
        accidental data loss (OWASP A01-003). When ``confirm`` is False,
        no rows are deleted — if ``dry_run`` is True the count that *would*
        be deleted is returned, otherwise 0 is returned.

        The scope is limited to SDK-created memories via
        ``source_file LIKE 'sdk-%'``.

        Args:
            confirm: Must be True to actually execute the DELETE. Otherwise
                no deletion occurs.
            dry_run: When True (and confirm is False), return the count that
                would be deleted without deleting anything.
        """
        from infra.db_write_queue import sqlite_write_queue

        conn = sqlite_write_queue.start_session(Path(self._db_path))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            pending_row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_file LIKE 'sdk-%' AND tenant_id = tenant_id()"
            ).fetchone()
            pending = pending_row[0] if pending_row is not None else 0
            if not confirm:
                logger.warning(
                    "Memory.clear() called without confirm=True; "
                    "no SDK memories were deleted (would have cleared %d).",
                    int(pending),
                )
                return int(pending) if dry_run else 0
            n = conn.execute(
                "DELETE FROM memories WHERE source_file LIKE 'sdk-%' AND tenant_id = tenant_id()"
            ).rowcount
            conn.commit()
            cleared = int(n) if n is not None else 0
            logger.info("Memory.clear() confirmed: cleared %d SDK memories.", cleared)
            return cleared
        finally:
            conn.close()

    def stats(self) -> dict:
        """Return memory and vector index stats."""
        from infra._lazy_imports import connection_pool, safe_close_db

        conn = connection_pool.get(str(self._db_path), timeout=10.0)
        try:
            return {
                "memories": conn.execute(
                    "SELECT COUNT(*) FROM tenant_memories WHERE deleted_at IS NULL"
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
        principal_id: str | None = None,
    ):
        from agent_context import init_agent

        self._ctx = init_agent(
            agent_id=agent_id,
            display_name=display_name or agent_id,
            parent_agent=parent_agent,
        )
        self._mem = Memory(db_path=db_path, principal_id=principal_id)
        self._principal_id = principal_id

    def save(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        is_global: bool = False,
    ) -> str:
        """Save a memory scoped to this agent.

        Args:
            content: The memory text.
            tags: Optional list of tags.
            is_global: If True, saves to the global (cross-project) memory
                scope. Defaults to False to keep agent memories scoped.
        """
        from agent_context import agent_save
        if self._principal_id:
            from agent_context import _AGENT_CONTEXT
            _AGENT_CONTEXT.principal_id = self._principal_id

        return str(agent_save(
            content=content,
            category="agents",
            title_slug=f"auto-{time.strftime('%Y%m%d_%H%M%S')}",
            tags=tags or [],
            is_global=is_global,
        ))

    def search(self, query: str, limit: int = 10, include_global: bool = False) -> list[dict]:
        """Search only this agent's memories."""
        from agent_context import agent_search

        result = agent_search(query=query, limit=limit, include_global=include_global)
        return [
            {
                "id": r.get("id", ""),
                "content": r.get("content", ""),
                "score": r.get("final_score", 0),
            }
            for r in result.get("results", [])
        ]
