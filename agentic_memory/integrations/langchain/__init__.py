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
