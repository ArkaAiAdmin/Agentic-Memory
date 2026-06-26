"""CrewAI ecosystem adapters for agentic-memory.

Provides:
    - :class:`AgenticMemorySearchTool` — CrewAI ``BaseTool`` for searching memory
    - :class:`AgenticMemorySaveTool` — CrewAI ``BaseTool`` for saving memory
    - :class:`AgenticMemoryMemory` — drop-in ``memory`` slot for ``Crew``

All classes are lazy-guarded: importing without CrewAI installed returns
``None`` rather than raising.

Install::

    pip install agentic-memory[crewai]
"""

from __future__ import annotations
