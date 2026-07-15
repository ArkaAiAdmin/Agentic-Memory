"""AgenticMemoryRetriever — LangChain BaseRetriever adapter.

Wraps :class:`agentic_memory.MemoryClient.search` behind LangChain's
``BaseRetriever`` interface so it can be dropped into any retrieval chain::

    from agentic_memory.integrations.langchain.retriever import (
        AgenticMemoryRetriever,
    )
    retriever = AgenticMemoryRetriever(
        db_path="~/.config/agentic-memory/memory/memory.db"
    )
    chain = RetrievalQA.from_chain_type(llm, retriever=retriever)

Requires::

    pip install agentic-memory[langchain]
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.retrievers import BaseRetriever
from pydantic import Field


class AgenticMemoryRetriever(BaseRetriever):
    """Adapts MemoryClient.search() into LangChain's retriever interface.

    Inherits from LangChain ``BaseRetriever`` which is itself a Pydantic
    ``Runnable`` subclass.
    """

    db_path: str | None = Field(
        default=None,
        description="Path to the agentic-memory SQLite database. Falls back to "
        "AGENTIC_MEMORY_DB_PATH env var if not set.",
    )
    search_kwargs: dict[str, Any] = Field(
        default_factory=lambda: {"limit": 5, "rerank": True},
        description="Keyword arguments forwarded to MemoryClient.search().",
    )

    def _resolve_db_path(self) -> str | None:
        if self.db_path:
            return self.db_path
        return os.environ.get("AGENTIC_MEMORY_DB_PATH")

    def _get_relevant_documents(self, query: str) -> list[Any]:
        from agentic_memory import MemoryClient

        db_path = self._resolve_db_path()
        mc = MemoryClient(db_path=db_path) if db_path else MemoryClient()
        _results = mc.search(query, **self.search_kwargs)

        return [self._to_document(r) for r in _results.results]

    def _to_document(self, r) -> Any:
        from langchain_core.documents import Document

        meta: dict[str, Any] = {
            "memory_id": r.id,
            "tags": r.tags,
            "category": r.category,
            "score": r.score,
            "created_at": r.created_at,
            "pinned": r.pinned,
            "importance": r.importance,
        }
        meta.update(
            {
                k: v
                for k, v in r.metadata.items()
                if v not in ("", [], 0, 0.0, False, None)
            }
        )
        return Document(page_content=r.content, metadata=meta)
