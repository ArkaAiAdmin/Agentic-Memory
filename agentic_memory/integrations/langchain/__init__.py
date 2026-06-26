"""LangChain ecosystem adapters for agentic-memory.

Provides:
    - :class:`AgenticMemoryRetriever` — LangChain ``BaseRetriever``
    - :class:`AgenticMemoryChatHistory` — LangChain ``BaseChatMessageHistory``
    - :func:`search_tool`, :func:`save_tool` — LangChain ``StructuredTool`` instances
    - :class:`AgenticMemoryCallbackHandler` — saves LLM turns to memory

All classes are lazy-guarded: importing this module without the relevant
LangChain packages installed returns ``None`` rather than raising.

Install::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations
