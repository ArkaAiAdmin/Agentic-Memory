"""AgenticMemoryMemory — CrewAI crew memory slot adapter.

Drops into any crew's ``memory`` slot to persist task context::

    from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory
    crew = Crew(
        agents=[researcher],
        tasks=[task],
        memory=AgenticMemoryMemory(db_path="..."),
    )

Supports both CrewAI v0.x and v1.x. v0.x calls ``save(context, agent, task)`` /
``search(query)``; v1.x calls ``remember(content, categories=...)`` /
``recall(query, categories=..., limit=...)``. All four methods share the same
``MemoryClient`` backend so data written via either interface is visible to
both.

v1.x requirements::

    pip install agentic-memory[crewai]
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_memory import MemoryClient


class AgenticMemoryMemory(BaseModel):
    """Drop-in memory adapter for CrewAI crews.

    Wraps :class:`agentic_memory.MemoryClient` behind the v0 ``save`` /
    ``search`` protocol AND the v1 ``remember`` / ``recall`` unified-memory
    protocol. The ``memory_kind`` field lets Pydantic discriminated unions
    (used in CrewAI v1's ``Crew.memory`` field) identify this as the base
    memory variant.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    memory_kind: Literal["memory"] = Field(
        default="memory",
        description="Discriminator for Pydantic tagged unions (CrewAI v1).",
    )
    db_path: str | None = Field(
        default=None,
        description="Path to the agentic-memory SQLite database.",
    )
    auto_tags: list[str] = Field(
        default_factory=list,
        description="Extra tags appended to every saved memory entry.",
    )
    read_only: bool = Field(
        default=False,
        description="When True, remember() is a no-op (matches v1 Memory protocol).",
    )

    def _resolve_db_path(self) -> str | None:
        if self.db_path:
            return self.db_path
        return os.environ.get("AGENTIC_MEMORY_DB_PATH") or os.environ.get(
            "MEMORY_DB_PATH"
        )

    def _make_client(self) -> MemoryClient:
        return MemoryClient(db_path=self._resolve_db_path())

    # ── v0.x compatibility ────────────────────────────────────────────────

    def save(self, context: str, agent: str = "", task: str = "") -> str:
        """Persist a crew task context entry (CrewAI v0.x interface).

        Tags the entry with ``crew`` plus the agent and task identifiers.
        Returns the note id string.

        .. deprecated::
            Use :meth:`remember` when targeting CrewAI v1.x.
        """
        tags = ["crew", agent, task] + self.auto_tags
        return self._make_client().save(context, tags=tags, category="crew")

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return memory entries relevant to the query (CrewAI v0.x interface).

        Returns plain dicts with ``content``, ``score``, ``tags``,
        ``category``, and ``created_at`` keys — matching the v0
        ``search() -> list[dict]`` contract.
        """
        mc = self._make_client()
        results = mc.search(query, limit=limit)
        return [
            {
                "content": r.content,
                "score": r.score,
                "tags": r.tags,
                "category": getattr(r, "category", ""),
                "created_at": getattr(r, "created_at", ""),
            }
            for r in results.results
        ]

    # ── v1.x protocol ─────────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        scope: str | None = None,
        categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        importance: float | None = None,
        source: str | None = None,
        private: bool = False,
        agent_role: str | None = None,
        root_scope: str | None = None,
    ) -> dict[str, Any]:
        """Store a single item in memory (CrewAI v1.x interface).

        Mirrors ``crewai.memory.Memory.remember``. All context is persisted
        via ``MemoryClient.save``; the returned dict has the fields CrewAI v1
        expects (``id``, ``content``, ``scope``, ``categories``, ``metadata``,
        ``importance``, ``created_at``, ``last_accessed``, ``source``,
        ``private``).

        Parameters match the v1 signature so this adapter can be passed
        directly into ``Crew(memory=...).``
        """
        if self.read_only:
            return {}

        mc = self._make_client()

        tags = list(categories) if categories else []
        if source:
            tags.append(f"source:{source}")
        if agent_role:
            tags.append(f"role:{agent_role}")
        if metadata is None:
            metadata = {}
        metadata["scope"] = scope or "/"
        if agent_role:
            metadata["agent_role"] = agent_role
        if root_scope:
            metadata["root_scope"] = root_scope

        cat = metadata.pop("category", "crew")
        importance_val = importance if importance is not None else metadata.pop(
            "importance", 0.5
        )

        note_id = mc.save(
            content,
            tags=tags + self.auto_tags,
            category=cat,
        )

        now = datetime.now(timezone.utc).isoformat()
        return {
            "id": note_id,
            "content": content,
            "scope": scope or (root_scope or "/"),
            "categories": tags,
            "metadata": metadata,
            "importance": importance_val if importance_val is not None else 0.5,
            "created_at": now,
            "last_accessed": now,
            "source": source,
            "private": private,
        }

    def recall(
        self,
        query: str,
        scope: str | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
        depth: Literal["shallow", "deep"] = "deep",
        source: str | None = None,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memories (CrewAI v1.x interface).

        Mirrors ``crewai.memory.Memory.recall``. Performs a semantic search
        via ``MemoryClient.search`` and returns ``MemoryMatch``-compatible
        dicts with ``record`` and ``score`` keys.

        ``depth`` is accepted for API compatibility but falls back to
        shallow semantic search regardless of value (the agentic-memory
        pipeline already handles composite relevance scoring internally).
        """
        mc = self._make_client()
        tags = categories if categories else None
        results = mc.search(query, limit=limit, tags=tags)

        matches: list[dict[str, Any]] = []
        for r in results.results:
            if not r.content:
                continue
            record: dict[str, Any] = {
                "id": getattr(r, "id", ""),
                "content": r.content,
                "scope": scope or "/",
                "categories": r.tags or [],
                "metadata": {},
                "importance": float(getattr(r, "importance", 3)),
                "created_at": getattr(r, "created_at", ""),
                "last_accessed": getattr(r, "created_at", ""),
                "source": source,
                "private": not include_private,
            }
            matches.append({"record": record, "score": r.score})

        return matches

    def remember_many(
        self,
        contents: list[str],
        scope: str | None = None,
        categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        importance: float | None = None,
        source: str | None = None,
        private: bool = False,
        agent_role: str | None = None,
        root_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Remember multiple items (CrewAI v1.x interface).

        Calls :meth:`remember` for each content string.
        """
        return [
            self.remember(
                c,
                scope=scope,
                categories=categories,
                metadata=metadata,
                importance=importance,
                source=source,
                private=private,
                agent_role=agent_role,
                root_scope=root_scope,
            )
            for c in contents
        ]

    def drain_writes(self) -> None:
        """Flush any pending async writes (CrewAI v1.x interface).

        AgenticMemoryMemory synchronously persists on every ``remember`` call,
        so there are no pending writes to drain. This is a no-op that satisfies
        the v1 ``Memory`` protocol expected by ``Crew.kickoff()``.
        """
        return None

    def reset(self, scope: str | None = None) -> None:
        """Reset memories within the given scope (CrewAI v1.x interface).

        AgenticMemoryMemory does not persist reset state across sessions, so
        this is a best-effort no-op. It satisfies the v1 protocol so the
        adapter can be used as a drop-in ``Crew.memory`` replacement.
        """
        return None

    def forget(
        self,
        scope: str | None = None,
        categories: list[str] | None = None,
        older_than: datetime | None = None,
        metadata_filter: dict[str, Any] | None = None,
        record_ids: list[str] | None = None,
    ) -> int:
        """Delete memories matching criteria (CrewAI v1.x interface).

        Backed by ``MemoryClient``; raises ``NotImplementedError`` unless
        CrewAI v1 directly calls ``forget`` during normal operation — which
        it does not for crew task execution.
        """
        raise NotImplementedError(
            "AgenticMemoryMemory does not implement forget(); "
            "use agentic_memory.MemoryClient.delete(note_id) directly."
        )
