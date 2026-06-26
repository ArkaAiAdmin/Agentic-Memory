"""AgenticMemoryMemory — CrewAI crew memory slot adapter.

Drops into any crew's ``memory`` slot to persist task context::

    from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory
    crew = Crew(
        agents=[researcher],
        tasks=[task],
        memory=AgenticMemoryMemory(db_path="..."),
    )

CrewAI v0.x exposes a ``Memory`` protocol with ``save`` and ``search``
methods. v1.x renamed this to ``LongTermMemory`` / ``ShortTermMemory``.
A version check at construction time gives a clear error if an
unsupported CrewAI major version is installed.

Requires::

    pip install agentic-memory[crewai]
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentic_memory import MemoryClient


_MIN_SUPPORTED_MAJOR = 0
_MAX_SUPPORTED_MAJOR = 0  # bumped to 1 when v1 Memory protocol is verified


def _check_crewai_version() -> None:
    try:
        import crewai

        version = getattr(crewai, "__version__", "0.0.0")
        major = int(version.split(".")[0])
        if not (_MIN_SUPPORTED_MAJOR <= major <= _MAX_SUPPORTED_MAJOR):
            raise ImportError(
                f"AgenticMemoryMemory supports CrewAI v{_MIN_SUPPORTED_MAJOR}.x "
                f"(detected v{version}). "
                f"CrewAI v1.x changed the Memory protocol — open an issue at "
                f"github.com/ArkaAiAdmin/Agentic-Memory to request v1 support, "
                f"or pin crewai to '>=0.80,<1.0'."
            )
    except ImportError:
        # Let the caller discover the missing package via the normal
        # ImportError on `from crewai.tools import BaseTool` etc.
        pass


class AgenticMemoryMemory(BaseModel):
    """Drop-in memory adapter for CrewAI crews.

    Wraps :class:`agentic_memory.MemoryClient` behind the ``save`` /
    ``search`` protocol expected by CrewAI's crew memory slot.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db_path: str | None = Field(
        default=None,
        description="Path to the agentic-memory SQLite database.",
    )
    auto_tags: list[str] = Field(
        default_factory=list,
        description="Extra tags appended to every saved memory entry.",
    )

    def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
        _check_crewai_version()
        super().__init__(db_path=db_path, **kwargs)

    def _resolve_db_path(self) -> str | None:
        if self.db_path:
            return self.db_path
        return os.environ.get("AGENTIC_MEMORY_DB_PATH")

    def save(self, context: str, agent: str = "", task: str = "") -> None:
        """Persist a crew task context entry.

        Mirrors CrewAI's memory ``save(context, agent, task)`` contract.
        Tags the entry with ``crew`` plus the agent and task identifiers
        so it is filterable and discoverable later.
        """
        mc = MemoryClient(db_path=self._resolve_db_path())
        mc.save(
            context,
            category="crew",
            tags=["crew", agent, task] + self.auto_tags,
        )

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return memory entries relevant to the query.

        Mirrors CrewAI's memory ``search(query) -> list[dict]`` contract.
        Returns plain dicts (not SDK model objects) so the crew runner
        can serialise them without SDK imports.
        """
        mc = MemoryClient(db_path=self._resolve_db_path())
        results = mc.search(query, limit=limit)
        return [
            {
                "content": r.content,
                "score": r.score,
                "tags": r.tags,
                "category": r.category,
                "created_at": r.created_at,
            }
            for r in results.results
        ]
