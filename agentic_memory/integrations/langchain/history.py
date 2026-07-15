"""AgenticMemoryChatHistory — LangChain BaseChatMessageHistory adapter.

Wraps :class:`agentic_memory.MemoryClient.save` to store LangChain
``BaseMessage`` objects as tagged session memories::

    from agentic_memory.integrations.langchain.history import (
        AgenticMemoryChatHistory,
    )
    history = AgenticMemoryChatHistory(db_path="...", session_id="session-1")
    history.add_message(HumanMessage(content="Hello"))
    history.add_message(AIMessage(content="Hi there!"))

Requires::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations

import os
from typing import Any

try:
    from langchain_core.chat_history import BaseChatMessageHistory
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
except ImportError:
    BaseChatMessageHistory = object  # type: ignore[misc,assignment]
    AIMessage = HumanMessage = SystemMessage = type(None)  # type: ignore[misc,assignment]


def _role_tag(message: Any) -> str:
    try:
        if isinstance(message, HumanMessage):
            return "human"
        if isinstance(message, AIMessage):
            return "ai"
        if isinstance(message, SystemMessage):
            return "system"
    except NameError:
        pass
    return "message"


class AgenticMemoryChatHistory(BaseChatMessageHistory):
    """Stores LangChain ``BaseMessage`` objects as agentic-memory saves.

    Each call to ``add_message`` calls :meth:`MemoryClient.save` with the
    message content tagged by role and session ID.

    ``clear()`` is a no-op until session-scoped deletion is implemented in
    the underlying pipeline (see follow-up task).
    """

    def __init__(
        self,
        db_path: str | None = None,
        session_id: str = "default",
    ) -> None:
        self.db_path = db_path
        self.session_id = session_id
        self.messages: list[Any] = []

    def _resolve_db_path(self) -> str | None:
        if self.db_path:
            return self.db_path
        return os.environ.get("AGENTIC_MEMORY_DB_PATH")

    def add_message(self, message: Any) -> None:
        from agentic_memory import MemoryClient

        db_path = self._resolve_db_path()
        mc = MemoryClient(db_path=db_path) if db_path else MemoryClient()
        role = _role_tag(message)
        mc.save(
            message.content,
            category="sessions",
            tags=[role, self.session_id],
        )
        self.messages.append(message)

    def clear(self) -> None:
        """Soft-delete all memories for this session.

        Deferred to follow-up — requires a session-scoped delete in the
        underlying pipeline. No-op for now to preserve caller safety.
        """
        self.messages.clear()
