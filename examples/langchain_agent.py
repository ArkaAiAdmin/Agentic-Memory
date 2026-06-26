"""LangChain agent with agentic-memory as a retriever and tools.

Run with:
    pip install agentic-memory[langchain]
    python examples/langchain_agent.py

Requires ANTHROPIC_API_KEY in the environment for ChatAnthropic.
Falls back to a no-op LLM print path if no key is present.
"""

from __future__ import annotations

import os
import sys

from agentic_memory.integrations.langchain.retriever import AgenticMemoryRetriever
from agentic_memory.integrations.langchain.tool import search_tool, save_tool


def main() -> int:
    db_path = os.environ.get(
        "AGENTIC_MEMORY_DB_PATH",
        os.path.expanduser("~/.config/agentic-memory/memory/memory.db"),
    )

    print(f"DB: {db_path}")

    # Seed some memories for the demo
    from agentic_memory import MemoryClient

    mc = MemoryClient(db_path=db_path)
    mc.save("User prefers dark mode for UI", tags=["preferences", "ui"], pinned=True)
    mc.save("User is building an AI agent memory system", tags=["project"])
    mc.save("Python 3.11 is the minimum supported version", tags=["technical"])
    mc.save(
        "User prefers TypeScript over JavaScript for new frontend work",
        tags=["preferences", "frontend"],
    )
    print("Seeded 4 demo memories.\n")

    # ── 1. Direct retriever usage ────────────────────────────────────────────
    print("── Retriever demo ──────────────────────────────────────────────")
    retriever = AgenticMemoryRetriever(
        db_path=db_path,
        search_kwargs={"limit": 3, "rerank": True},
    )
    docs = retriever.invoke("What does the user prefer for UI?")
    for i, doc in enumerate(docs, 1):
        print(f"  [{i}] score={doc.metadata.get('score', 0):.3f}  {doc.page_content}")
    print()

    # ── 2. Search + save tools demo ──────────────────────────────────────────
    print("── Tool demo ───────────────────────────────────────────────────")
    result = search_tool.invoke({"query": "Python version"})
    print(f"  Search result:\n    {result}\n")

    result = save_tool.invoke(
        {
            "content": "User runs agentic-memory on macOS with Python 3.14",
            "tags": ["environment"],
            "category": "sessions",
        }
    )
    print(f"  Save result: {result}\n")

    # ── 3. Verify retrieval ──────────────────────────────────────────────────
    print("── Post-save retrieval ─────────────────────────────────────────")
    docs2 = retriever.invoke("macOS environment")
    for i, doc in enumerate(docs2, 1):
        print(f"  [{i}] {doc.page_content}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
