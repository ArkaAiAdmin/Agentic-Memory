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

from pydantic import BaseModel, ConfigDict, Field

from agentic_memory.models import MemoryResult  # noqa: F401 — re-export


def _role_tag(message: Any) -> str:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    if isinstance(message, HumanMessage):
        return "human"
    if isinstance(message, AIMessage):
        return "ai"
    if isinstance(message, SystemMessage):
        return "system"
    return "message"


class AgenticMemoryChatHistory(BaseModel):
    """Stores LangChain ``BaseMessage`` objects as agentic-memory saves.

    Each call to ``add_message`` calls :meth:`MemoryClient.save` with the
    message content tagged by role and session ID.

    ``clear()`` is a no-op until session-scoped deletion is implemented in
    the underlying pipeline (see follow-up task).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db_path: str | None = Field(
        default=None,
        description="Path to the agentic-memory SQLite database. "
        "Falls back to AGENTIC_MEMORY_DB_PATH env var.",
    )
    session_id: str = Field(
        default="default",
        description="Session identifier used as a tag for look-back.",
    )
    messages: list[Any] = Field(
        default_factory=list,
        description="In-memory message cache (not the source of truth).",
    )

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
