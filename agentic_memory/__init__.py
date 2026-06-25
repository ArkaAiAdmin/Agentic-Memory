"""Agentic Memory — Local-first persistent memory for AI agents.

This package is the canonical, pip-installable surface of the agentic-memory
system. It re-exports the Mem0-compatible SDK (`Memory`, `AgentMemory`) and
ships with a CLI entry point so the package can be used from any Python
project after `pip install agentic-memory`.

Quick start::

    from agentic_memory import Memory

    m = Memory()
    m.add("User prefers dark mode")
    results = m.search("What does the user prefer?")

    # With agent scoping:
    from agentic_memory import AgentMemory

    am = AgentMemory(agent_id="coder-1")
    am.save("Frontend uses React with TypeScript")
    results = am.search("frontend")

CLI usage (after `pip install -e .`)::

    agentic-memory search "user preferences"
    agentic-memory add "User prefers dark mode"
    agentic-memory list
    agentic-memory stats

Module usage::

    python -m agentic_memory search "user preferences"

See Also:
    - `sdk.py` — implementation of `Memory` / `AgentMemory`.
    - `setup_memory.sh` — bootstraps a fresh project to use this package.
    - `examples/` — runnable example scripts.
"""

__version__ = "1.0.0"

import os
import sys
from pathlib import Path

# Make the repository root importable so `sdk.py` (which lives at the top
# level and uses sibling modules like `_lazy_imports`, `memory_delete`,
# `agent_context`) can find its dependencies. When the package is installed
# via pip from a real distribution, the source layout is different and
# this hack is unnecessary; in editable/dev mode where we run from the
# repo root, this shim keeps things working.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-export the public API. We import from the top-level `sdk.py` module
# (which already exists and is the source of truth) so the legacy
# `from sdk import Memory` import path keeps working for existing tests
# (eval/test_all_extended.py, eval/test_sdk.py, etc.).
from sdk import Memory, AgentMemory  # noqa: E402,F401

__all__ = [
    "Memory",
    "AgentMemory",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Provides a small CLI so the package is usable end-to-end without the
    MCP server. Dispatches to the same operations exposed by the
    `Memory` class.

    Usage::

        agentic-memory add <text> [tags...]
        agentic-memory search <query> [--limit N]
        agentic-memory list [--limit N]
        agentic-memory stats
        agentic-memory clear
    """
    import argparse
    import json as _json

    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="agentic-memory",
        description="Agentic Memory CLI — quick search/save via the SDK",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Save a memory")
    p_add.add_argument("text", help="Memory text to save")
    p_add.add_argument("tags", nargs="*", help="Optional tags")

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Query string")
    p_search.add_argument("--limit", type=int, default=5, help="Result limit")

    p_list = sub.add_parser("list", help="List recent memories")
    p_list.add_argument("--limit", type=int, default=10, help="How many to list")

    sub.add_parser("stats", help="Print DB stats")
    sub.add_parser("clear", help="Clear all SDK-created memories")

    p_demo = sub.add_parser("demo", help="Run a quick end-to-end demo")
    p_demo.add_argument(
        "--query",
        default="preferences",
        help="Query to search for at the end of the demo",
    )

    args = parser.parse_args(argv)

    try:
        m = Memory()
    except Exception as exc:
        print(f"agentic-memory: failed to initialize Memory: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "add":
        note_id = m.add(args.text, tags=args.tags)
        print(_json.dumps({"note_id": note_id}))
        return 0
    if args.cmd == "search":
        results = m.search(args.query, limit=args.limit)
        print(_json.dumps(results, indent=2, default=str))
        return 0
    if args.cmd == "list":
        notes = m.list(limit=args.limit)
        print(_json.dumps({"count": len(notes), "notes": notes}, indent=2, default=str))
        return 0
    if args.cmd == "stats":
        print(_json.dumps(m.stats(), indent=2))
        return 0
    if args.cmd == "clear":
        n = m.clear()
        print(_json.dumps({"cleared": n}))
        return 0
    if args.cmd == "demo":
        return _run_demo(args.query)

    parser.print_help()
    return 1


def _run_demo(query: str) -> int:
    """Quick end-to-end demo: save a few notes, search, print results.

    This is the canonical "hello world" for the SDK. It is the
    implementation backing the `memory_sdk_demo` MCP tool.
    """
    import json as _json

    m = Memory()
    samples = [
        ("User prefers dark mode in all editors.", ["preferences", "ui"]),
        ("User is learning Rust and building a CLI tool.", ["learning", "rust"]),
        ("Project uses PostgreSQL with the pgvector extension.", ["database"]),
    ]
    saved = []
    for text, tags in samples:
        try:
            note_id = m.add(text, tags=tags)
            saved.append({"text": text, "note_id": note_id})
        except Exception as exc:
            saved.append({"text": text, "error": str(exc)})

    try:
        results = m.search(query, limit=5)
    except Exception as exc:
        results = [{"error": str(exc)}]

    try:
        stats = m.stats()
    except Exception as exc:
        stats = {"error": str(exc)}

    out = {
        "saved": saved,
        "search_query": query,
        "results": results,
        "stats": stats,
    }
    print(_json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
