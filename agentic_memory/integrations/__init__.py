"""Agentic Memory — ecosystem integrations.

Ecosystem adapters exposing agentic-memory to LangChain, CrewAI, and
future frameworks. All imports are lazy-guarded: the core package works
without any integration dependency installed.

Install extras
--------------
.. code-block:: bash

    pip install agentic-memory[langchain]   # LangChain adapters
    pip install agentic-memory[crewai]      # CrewAI adapters
    pip install agentic-memory[all]         # Both
"""

from __future__ import annotations

# ── LangChain ────────────────────────────────────────────────────────────────


def __get_langchain_retriever():  # type: ignore[return]
    try:
        from agentic_memory.integrations.langchain.retriever import (
            AgenticMemoryRetriever,
        )

        return AgenticMemoryRetriever
    except ImportError:
        return None


def __get_langchain_history():  # type: ignore[return]
    try:
        from agentic_memory.integrations.langchain.history import (
            AgenticMemoryChatHistory,
        )

        return AgenticMemoryChatHistory
    except ImportError:
        return None


def __get_langchain_tools():
    try:
        from agentic_memory.integrations.langchain.tool import (
            search_tool,
            save_tool,
        )

        return search_tool, save_tool
    except ImportError:
        return None, None


def __get_langchain_callback():  # type: ignore[return]
    try:
        from agentic_memory.integrations.langchain.callback import (
            AgenticMemoryCallbackHandler,
        )

        return AgenticMemoryCallbackHandler
    except ImportError:
        return None


# ── CrewAI ───────────────────────────────────────────────────────────────────


def __get_crewai_search_tool():  # type: ignore[return]
    try:
        from agentic_memory.integrations.crewai.tool import (
            AgenticMemorySearchTool,
        )

        return AgenticMemorySearchTool
    except ImportError:
        return None


def __get_crewai_save_tool():  # type: ignore[return]
    try:
        from agentic_memory.integrations.crewai.tool import (
            AgenticMemorySaveTool,
        )

        return AgenticMemorySaveTool
    except ImportError:
        return None


def __get_crewai_memory():  # type: ignore[return]
    try:
        from agentic_memory.integrations.crewai.memory import (
            AgenticMemoryMemory,
        )

        return AgenticMemoryMemory
    except ImportError:
        return None
