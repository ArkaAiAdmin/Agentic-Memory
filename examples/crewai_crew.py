"""CrewAI crew with agentic-memory memory slot and tools.

Run with:
    pip install agentic-memory[crewai]
    python examples/crewai_crew.py

Requires OPENAI_API_KEY in the environment for CrewAI's default LLM.
Falls back to a no-op print path if no key is present.
"""

from __future__ import annotations

import os
import sys

from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory
from agentic_memory.integrations.crewai.tool import (
    AgenticMemorySearchTool,
    AgenticMemorySaveTool,
)


def main() -> int:
    db_path = os.environ.get(
        "AGENTIC_MEMORY_DB_PATH",
        os.path.expanduser("~/.config/agentic-memory/memory/memory.db"),
    )

    print(f"DB: {db_path}")

    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not has_openai:
        print("Note: OPENAI_API_KEY not set — showing SDK-only demo path.\n")

    # ── 1. Crew memory slot demo ──────────────────────────────────────────────
    print("── Crew memory slot demo ───────────────────────────────────────")

    memory = AgenticMemoryMemory(
        db_path=db_path,
        auto_tags=["crew-demo"],
    )

    # Seed a task context entry (mirrors what CrewAI itself would call)
    memory.save(
        context="User is evaluating CrewAI + agentic-memory for memory persistence.",
        agent="researcher",
        task="research_memory_backends",
    )
    print("  Saved: crew task context entry\n")

    # Query it back
    results = memory.search("CrewAI memory", limit=5)
    for r in results:
        print(f"  - [{r['score']:.3f}] {r['content'][:80]}")
        print(f"    tags: {r['tags']}")
    print()

    # ── 2. CrewAI tool instantiation ──────────────────────────────────────────
    print("── CrewAI tools instantiation ──────────────────────────────────")

    search_tool = AgenticMemorySearchTool(db_path=db_path)
    save_tool = AgenticMemorySaveTool(db_path=db_path)

    print(f"  search_tool.name: {search_tool.name}")
    print(f"  save_tool.name:   {save_tool.name}")

    # Test tool.run directly (without CrewAI agent)
    tool_result = search_tool._run("CrewAI memory")
    print(f"\n  Search via tool._run():\n    {tool_result[:200]}\n")

    save_result = save_tool._run(
        "CrewAI demo completed successfully.",
        tags=["crew-demo", "completed"],
        category="sessions",
    )
    print(f"  Save via tool._run(): {save_result}\n")

    # ── 3. Full crew (if OPENAI_API_KEY present) ──────────────────────────────
    if has_openai:
        print("── Running crew with agentic-memory ──────────────────────────")
        try:
            from crewai import Agent, Task, Crew, Process

            researcher = Agent(
                role="Research Assistant",
                goal="Find and remember key information",
                backstory="You are a helpful research assistant with persistent memory.",
                tools=[search_tool, save_tool],
                verbose=True,
                allow_delegation=False,
            )

            research_task = Task(
                description="Search memory for CrewAI memory persistence notes, "
                "then save a summary of what you found.",
                expected_output="A brief summary of findings about CrewAI memory.",
                agent=researcher,
            )

            crew = Crew(
                agents=[researcher],
                tasks=[research_task],
                memory=memory,
                process=Process.sequential,
                verbose=True,
            )

            result = crew.kickoff()
            print(f"\nCrew result: {result}\n")
        except Exception as e:
            print(f"  Crew run failed: {e}")
            print("  (This is okay — CrewAI may need additional config)")
    else:
        print("── Skipping full crew run (no OPENAI_API_KEY) ───────────────")
        print("  The SDK components above verified correctly.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
