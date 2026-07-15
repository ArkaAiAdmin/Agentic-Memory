"""LangChain ecosystem adapters for agentic-memory.

Provides:
    - :class:`AgenticMemoryRetriever` — LangChain ``BaseRetriever`` subclass
    - :class:`AgenticMemoryChatHistory` — LangChain ``BaseChatMessageHistory`` subclass
    - :func:`search_tool`, :func:`save_tool` — LangChain ``StructuredTool`` instances
    - :class:`AgenticMemoryCallbackHandler` — LangChain ``BaseCallbackHandler`` subclass

All adapter classes properly subclass their respective LangChain base
classes for seamless integration with chains, agents, and the LangChain
ecosystem.

Install::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations

try:
    from agentic_memory.integrations.langchain.retriever import (
        AgenticMemoryRetriever,
    )
    from agentic_memory.integrations.langchain.history import (
        AgenticMemoryChatHistory,
    )
    from agentic_memory.integrations.langchain.callback import (
        AgenticMemoryCallbackHandler,
    )
except ImportError as _e:
    raise ImportError(
        "langchain-core is required for LangChain integrations. "
        "Install with: pip install agentic-memory[langchain]"
    ) from _e

__all__ = [
    "AgenticMemoryRetriever",
    "AgenticMemoryChatHistory",
    "AgenticMemoryCallbackHandler",
]
