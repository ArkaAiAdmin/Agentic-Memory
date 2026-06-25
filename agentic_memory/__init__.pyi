"""Type stubs for the agentic_memory package.

Provides rich type information for IDE autocomplete and static type
checking without requiring the runtime to be installed. The runtime
classes live in `sdk.py`; this file describes their public shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

__version__: str

class Memory:
    """Mem0-compatible memory store.

    Minimal in-memory + on-disk facade over the agentic-memory system.
    Mirrors Mem0's 3-line API surface: ``add(text)`` and ``search(query)``.

    Examples:
        >>> m = Memory()
        >>> note_id = m.add("User prefers dark mode")
        >>> results = m.search("user preferences")
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        user_id: str = "default",
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize a Memory instance.

        Args:
            db_path: Path to a custom SQLite DB. Defaults to the
                global memory DB resolved from ``config.get_config()``.
            user_id: Logical user identifier. Currently stored on the
                instance but not yet propagated to writes (H13 audit
                finding — kept as the canonical fix point).
            config: Optional override config dict. Reserved.
        """
        ...

    def add(self, content: str, tags: Optional[list[str]] = None) -> str:
        """Add a memory and return its note ID.

        Args:
            content: The text to remember.
            tags: Optional list of tags for retrieval hints.

        Returns:
            The canonical note ID (e.g. ``"sdk/sdk-auto-20260622-..."``).
        """
        ...

    def search(
        self,
        query: str,
        limit: int = 10,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """Search memories by semantic relevance.

        Args:
            query: The natural-language query.
            limit: Maximum number of results to return.
            rerank: If True, applies the cross-encoder reranker.

        Returns:
            A list of result dicts with keys ``id``, ``content``,
            ``score``, and ``tags``.
        """
        ...

    def delete(self, note_id: str) -> bool:
        """Soft-delete a memory by note ID.

        Returns:
            True if the note was found and deleted.
        """
        ...

    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List recent memories (newest first)."""
        ...

    def clear(self) -> int:
        """Clear all SDK-created memories. Returns count cleared."""
        ...

    def stats(self) -> dict[str, int]:
        """Return memory and vector index stats.

        Returns:
            A dict with keys ``memories``, ``vector_keys``, and ``chunks``.
        """
        ...

class AgentMemory:
    """Agent-scoped memory with namespace isolation.

    Wraps :class:`Memory` with automatic agent context. Each agent's
    memories are isolated via the ``agent_context`` module.

    Examples:
        >>> am = AgentMemory(agent_id="coder-1")
        >>> am.save("Frontend uses React with TypeScript")
        >>> results = am.search("frontend")
    """

    def __init__(
        self,
        agent_id: str,
        display_name: str = "",
        parent_agent: Optional[str] = None,
        db_path: Optional[str | Path] = None,
    ) -> None:
        """Initialize an agent-scoped memory context.

        Args:
            agent_id: Globally unique agent identifier.
            display_name: Human-readable name (defaults to ``agent_id``).
            parent_agent: ID of the spawning agent, if any.
            db_path: Optional custom DB path.
        """
        ...

    def save(self, content: str, tags: Optional[list[str]] = None) -> str:
        """Save a memory scoped to this agent."""
        ...

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search only this agent's memories."""
        ...

def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for the agentic-memory package.

    Subcommands:
        - ``add <text> [tags...]``
        - ``search <query> [--limit N]``
        - ``list [--limit N]``
        - ``stats``
        - ``clear``
        - ``demo [--query Q]``

    Returns:
        The process exit code (0 on success).
    """
    ...
