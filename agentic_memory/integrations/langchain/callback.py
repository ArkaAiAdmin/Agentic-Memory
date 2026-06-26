"""AgenticMemoryCallbackHandler — auto-save LLM turns to memory.

Attach to any LangChain chain or agent to automatically persist prompts
and responses::

    from agentic_memory.integrations.langchain.callback import (
        AgenticMemoryCallbackHandler,
    )
    handler = AgenticMemoryCallbackHandler(db_path="...", save_responses=True)
    llm = ChatAnthropic(...).bind(callbacks=[handler])

Requires::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgenticMemoryCallbackHandler(BaseModel):
    """Auto-saves LLM prompts and responses to agentic-memory.

    Fires on ``on_llm_start`` (prompts) and ``on_llm_end`` (responses)
    so every reasoning trace is captured without caller changes.

    Attributes:
        db_path: Database path; falls back to ``AGENTIC_MEMORY_DB_PATH``.
        save_prompts: Persist raw LLM input prompts (default ``False``).
        save_responses: Persist LLM output text (default ``True``).
        auto_tags: Tags appended to every auto-saved memory entry.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db_path: str | None = Field(
        default=None,
        description="Path to the agentic-memory SQLite database.",
    )
    save_prompts: bool = Field(
        default=False,
        description="Whether to persist LLM input prompts.",
    )
    save_responses: bool = Field(
        default=True,
        description="Whether to persist LLM output text.",
    )
    auto_tags: list[str] = Field(
        default_factory=lambda: ["auto-saved"],
        description="Tags appended to every auto-saved memory entry.",
    )

    def _resolve_db_path(self) -> str | None:
        if self.db_path:
            return self.db_path
        return os.environ.get("AGENTIC_MEMORY_DB_PATH")

    def _save(self, content: str, role_tag: str) -> None:
        from agentic_memory import MemoryClient

        db_path = self._resolve_db_path()
        mc = MemoryClient(db_path=db_path) if db_path else MemoryClient()
        mc.save(
            content,
            category="sessions",
            tags=self.auto_tags + [role_tag],
        )

    # LangChain CallbackHandler interface — on_llm_start and on_llm_end
    # are the two hooks that fire for every LLM call in a chain/agent run.

    def on_llm_start(self, serialized: Any, prompts: list[str], **kwargs: Any) -> None:
        if not self.save_prompts:
            return
        for prompt in prompts:
            if prompt and prompt.strip():
                self._save(prompt, "prompt")

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        if not self.save_responses:
            return
        try:
            generations = response.generations
        except AttributeError:
            return
        for gen_group in generations:
            for gen in gen_group:
                text = getattr(gen, "text", "")
                if text and text.strip():
                    self._save(text, "response")
