"""CrewAI tools wrapping agentic-memory search and save.

Provides two tool classes that expose agentic-memory as native crew tools::

    from agentic_memory.integrations.crewai.tool import (
        AgenticMemorySearchTool,
        AgenticMemorySaveTool,
    )
    agent = Agent(..., tools=[AgenticMemorySearchTool(db_path="...")])

Requires::

    pip install agentic-memory[crewai]
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentic_memory import MemoryClient

# ── BaseTool import ─────────────────────────────────────────────────────────
# CrewAI v0.x and v1.x both expose crewai.tools.BaseTool under different paths.
# Try both; fall back to None when crewai is not installed (the module is still
# importable and the tool classes degrade to plain duck-type tools).

try:
    from crewai.tools import BaseTool as _CrewaiBaseTool
except ImportError:
    try:
        from crewai.tools.base_tool import BaseTool as _CrewaiBaseTool
    except ImportError:
        _CrewaiBaseTool = None

# ── Dynamic base class selection ────────────────────────────────────────────
# When _CrewaiBaseTool is None (crewai not installed) we bind the tool classes
# to a plain object() sentinel __bases__ entry so the isinstance / issubclass
# checks in tests degrade gracefully without redefining the class name.

_CREWAI_BASES: tuple[type, ...]
if _CrewaiBaseTool is not None:
    _CREWAI_BASES = (_CrewaiBaseTool,)
else:
    _CREWAI_BASES = ()


def _make_tool_base(db_path_attr: str | None = None) -> type:
    """Build a small MRO entry that carries db_path and calls super().__init__."""
    if _CrewaiBaseTool is None:
        return object  # type: ignore[return-value]

    class _Base(_CrewaiBaseTool):
        db_path: str | None = None

        def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
            self.db_path = db_path
            super().__init__(**kwargs)

    return _Base


_MEMORY_TOOL_BASE = _make_tool_base()


# ── Input schemas ───────────────────────────────────────────────────────────


class AgenticMemorySearchInput(BaseModel):
    query: str = Field(description="Search query to look up relevant memories")
    limit: int = Field(
        default=5, description="Number of results to return", ge=1, le=50
    )


class AgenticMemorySaveInput(BaseModel):
    content: str = Field(description="The memory content to save")
    tags: list[str] = Field(
        default_factory=list, description="Optional list of tag strings"
    )
    category: str = Field(
        default="sdk",
        description="Category: lessons, projects, decisions, preferences, sessions",
    )


# ── Shared formatter ────────────────────────────────────────────────────────


def _format_as_llm_readable(results: Any) -> str:
    """Convert SearchResults into a compact string a CrewAI agent can act on."""
    items = getattr(results, "results", [])
    total = getattr(results, "total", len(items))
    query_val = getattr(results, "query", "")

    lines = [f"[memory search: {total} results for '{query_val}']"]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. [score={r.score:.3f}] {r.content}")
        if r.tags:
            lines.append(f"   tags: {', '.join(r.tags)}")
    synthesis = getattr(results, "synthesis", "")
    if synthesis:
        lines.append(f"\n[synthesis]\n{synthesis}")
    return "\n".join(lines)


# ── Tool implementations ────────────────────────────────────────────────────

if _CrewaiBaseTool is not None:

    class AgenticMemorySearchTool(_MEMORY_TOOL_BASE):  # type: ignore[misc,valid-type]
        """CrewAI tool for searching persistent agent memory.

        Use when the agent needs to recall prior context about a user,
        project, or decision before acting.
        """

        name: str = "agentic_memory_search"
        description: str = "Search persistent agent memory by semantic relevance."
        args_schema: Any = AgenticMemorySearchInput

        def _run(self, query: str, limit: int = 5) -> str:
            mc = MemoryClient(db_path=self.db_path)
            results = mc.search(query, limit=limit)
            return _format_as_llm_readable(results)

        async def _arun(self, query: str, limit: int = 5) -> str:
            return self._run(query, limit=limit)

    class AgenticMemorySaveTool(_MEMORY_TOOL_BASE):  # type: ignore[misc,valid-type]
        """CrewAI tool for saving information to persistent agent memory.

        Use when the agent has just learned or decided something worth
        remembering across sessions.
        """

        name: str = "agentic_memory_save"
        description: str = "Save an important fact or observation to agent memory."
        args_schema: Any = AgenticMemorySaveInput

        def _run(
            self, content: str, tags: list[str] | None = None, category: str = "sdk"
        ) -> str:
            mc = MemoryClient(db_path=self.db_path)
            note_id = mc.save(content, tags=tags or [], category=category)
            return f"Saved as {note_id}"

        async def _arun(
            self, content: str, tags: list[str] | None = None, category: str = "sdk"
        ) -> str:
            return self._run(content, tags, category)

else:

    class AgenticMemorySearchTool:  # type: ignore[no-redef]
        """CrewAI tool (crewai not installed — plain class stub).

        Importing this module succeeds without crewai, but ``_run`` delegates
        to ``MemoryClient`` directly so calls still work.
        """

        name: str = "agentic_memory_search"
        description: str = "Search persistent agent memory by semantic relevance."
        args_schema: Any = AgenticMemorySearchInput
        db_path: str | None = None

        def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
            self.db_path = db_path

        def _run(self, query: str, limit: int = 5) -> str:
            mc = MemoryClient(db_path=self.db_path)
            return _format_as_llm_readable(mc.search(query, limit=limit))

        async def _arun(self, query: str, limit: int = 5) -> str:
            return self._run(query, limit=limit)

    class AgenticMemorySaveTool:  # type: ignore[no-redef]
        """CrewAI tool (crewai not installed — plain class stub)."""

        name: str = "agentic_memory_save"
        description: str = "Save an important fact or observation to agent memory."
        args_schema: Any = AgenticMemorySaveInput
        db_path: str | None = None

        def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
            self.db_path = db_path

        def _run(
            self, content: str, tags: list[str] | None = None, category: str = "sdk"
        ) -> str:
            mc = MemoryClient(db_path=self.db_path)
            note_id = mc.save(content, tags=tags or [], category=category)
            return f"Saved as {note_id}"

        async def _arun(
            self, content: str, tags: list[str] | None = None, category: str = "sdk"
        ) -> str:
            return self._run(content, tags, category)
