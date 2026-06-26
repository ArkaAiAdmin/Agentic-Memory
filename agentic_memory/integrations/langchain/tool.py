"""LangChain StructuredTools wrapping agentic-memory search and save.

Exposes two ready-to-use :class:`langchain_core.tools.StructuredTool`
instances::

    from agentic_memory.integrations.langchain.tool import search_tool, save_tool
    agent = create_react_agent(llm, tools=[search_tool, save_tool], ...)

Requires::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentic_memory import MemoryClient


# ── Input schemas ─────────────────────────────────────────────────────────────


class SaveMemoryInput(BaseModel):
    content: str = Field(description="The memory content to save")
    category: str = Field(
        default="sdk",
        description=(
            "Category: lessons, projects, decisions, preferences, sessions, sdk"
        ),
    )
    tags: list[str] | None = Field(
        default=None, description="Optional list of tag strings"
    )


class SearchMemoryInput(BaseModel):
    query: str = Field(description="Search query to look up relevant memories")
    limit: int = Field(
        default=5, description="Number of results to return", ge=1, le=50
    )


# ── Shared formatter ──────────────────────────────────────────────────────────


def _format_as_llm_readable(results: Any) -> str:
    """Convert SearchResults into a compact string an LLM can read."""
    items = getattr(results, "results", [])
    total = getattr(results, "total", len(items))
    query = getattr(results, "query", "")
    synthesis = getattr(results, "synthesis", "")

    lines = [f"[memory search: {total} results for '{query}']"]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. [score={r.score:.3f}] {r.content}")
        if r.tags:
            lines.append(f"   tags: {', '.join(r.tags)}")
    if synthesis:
        lines.append(f"\n[synthesis]\n{synthesis}")
    return "\n".join(lines)


# ── Tool implementations ──────────────────────────────────────────────────────


def agentic_memory_search(query: str, limit: int = 5) -> str:
    mc = MemoryClient()
    results = mc.search(query, limit=limit)
    return _format_as_llm_readable(results)


def agentic_memory_save(
    content: str, category: str = "sdk", tags: list[str] | None = None
) -> str:
    mc = MemoryClient()
    note_id = mc.save(content, category=category, tags=tags or [])
    return f"Saved as {note_id}"


try:
    from langchain_core.tools import StructuredTool

    search_tool: Any = StructuredTool.from_function(
        func=agentic_memory_search,
        name="agentic_memory_search",
        description=(
            "Search your persistent memory for relevant past context, "
            "decisions, and preferences."
        ),
        args_schema=SearchMemoryInput,
    )

    save_tool: Any = StructuredTool.from_function(
        func=agentic_memory_save,
        name="agentic_memory_save",
        description=(
            "Save an important fact or observation to persistent memory "
            "for future recall."
        ),
        args_schema=SaveMemoryInput,
    )
except ImportError:
    search_tool = None  # type: ignore[assignment]
    save_tool = None  # type: ignore[assignment]
