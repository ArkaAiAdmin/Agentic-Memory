"""
MCP tool: memory_sdk_demo.

Demonstrates how to use the agentic_memory SDK (`Memory`,
`AgentMemory`) end-to-end. Wraps the same `main()` function that
backs the `agentic-memory` CLI and the `python -m agentic_memory`
entry point.

This tool is the canonical "show me how to use the SDK" entry
point. It saves a few sample memories, runs a search, and prints
DB stats so the caller can verify the wiring works.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401
from typing import Any



import json

from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit


@mcp.tool()
@with_audit("memory_sdk_demo")
def memory_sdk_demo(query: str = "preferences", samples: int = 3) -> str:
    """Run an end-to-end SDK demo: save sample memories, search, and report stats.

    Wraps the same demo logic that backs the `agentic-memory demo`
    CLI subcommand. Use this when an MCP client wants to verify
    the SDK is wired correctly without touching the live memory
    store (the demo saves real notes — pass ``samples=0`` to skip
    the save phase).

    Args:
        query: The search query used to demonstrate the read path
            (default: ``"preferences"``).
        samples: How many sample memories to save. Set to ``0`` to
            skip the save phase entirely (useful for read-only
            checks).

    Returns:
        A JSON string with the save results, the search hits, and
        the live DB stats. On failure, an ``_err`` envelope.
    """
    try:
        # Inlined so the MCP tool can return a structured envelope.
        # The underlying logic is identical to the CLI's `demo`
        # subcommand and to ``agentic_memory._run_demo``.
        from agentic_memory import Memory

        m = Memory()
        all_samples = [
            ("User prefers dark mode in all editors.", ["preferences", "ui"]),
            ("User is learning Rust and building a CLI tool.", ["learning", "rust"]),
            ("Project uses PostgreSQL with the pgvector extension.", ["database"]),
            ("Frontend stack is React + TypeScript with Vite.", ["frontend", "react"]),
            ("Documentation uses MkDocs Material theme.", ["docs"]),
        ]
        sample_texts = all_samples[: max(0, min(samples, len(all_samples)))]

        saved = []
        for text, tags in sample_texts:
            try:
                note_id = m.add(text, tags=tags)
                saved.append({"text": text, "note_id": note_id})
            except Exception as exc:
                saved.append({"text": text, "error": str(exc)})

        results: list = []
        if query:
            try:
                results = m.search(query, limit=5)
            except Exception as exc:
                results = [{"error": str(exc)}]

        try:
            stats: dict[str, Any] = m.stats()
        except Exception as exc:
            stats = {"error": str(exc)}

        envelope = {
            "ok": True,
            "saved": saved,
            "search_query": query,
            "results": results,
            "stats": stats,
            "usage": {
                "from_python": "from agentic_memory import Memory, AgentMemory",
                "cli": "agentic-memory demo --query 'preferences'",
                "module": "python -m agentic_memory search 'preferences'",
            },
        }
        return json.dumps(envelope, indent=2, default=str)
    except Exception as exc:
        return _err(ErrorCode.QUALITY_ERROR, f"memory_sdk_demo failed: {exc}")
