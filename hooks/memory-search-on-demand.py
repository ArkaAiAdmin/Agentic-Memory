#!/usr/bin/env python3
"""On-demand memory search — CLI script and lazy import target.

This is a CLI / importable helper, NOT a Claude Code lifecycle hook.
The contradiction report (I4, 2026-06-22) called out that the
filename lives in ``hooks/`` and AGENTS.md listed it as one of the
4 hooks, but Claude Code never invokes it (it reads JSON from stdin,
not ``sys.argv``). Operators call it directly:

    venv/bin/python hooks/memory-search-on-demand.py "your query" [limit]

It can also be imported as a Python module:

    from memory_search_on_demand import search
    results = search("query", limit=5)

I10 fix (2026-06-22): imports ``search_memories`` from
``search.orchestrator`` (the canonical source) instead of going
through the ``search_pipeline`` re-export shim.

Directly calls search_memories for every search.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap_path  # noqa: E402

import json

# I10 fix: import directly from search.orchestrator.
from search.orchestrator import search_memories  # noqa: E402
from memory_common import get_memory_paths  # noqa: E402

# 2026-06-23: Removed ineffective in-memory _SEARCH_CACHE. CLI hooks
# are run as transient, separate Python subprocesses, meaning
# process-local caches do not persist across hook invocations.
# SQLite querying is direct and fast.


def search(query: str, limit: int = 5, db_path: Path | None = None) -> list[dict]:
    """Search memory and return structured results."""
    if db_path is None:
        _, local_mem, _ = get_memory_paths()
        db_path = local_mem / "memory.db"

    if not db_path.exists():
        return []

    results = search_memories(
        db_path=db_path, query=query, limit=limit, include_global=True
    )
    res = results.get("results", [])
    if not isinstance(res, list):
        res = []
    return res


def format_results(results: list[dict]) -> str:
    """Format for human-readable output."""
    if not results:
        return "No relevant memories found."

    out = []
    for i, r in enumerate(results, 1):
        content = r.get("content", "")[:400]
        score = r.get("final_score", 0)
        out.append(f"[{i}] {r.get('id')} (score: {score:.2f})\n    {content}...")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print(
            'Usage: python memory-search-on-demand.py "query" [limit]', file=sys.stderr
        )
        sys.exit(1)

    query = sys.argv[1]
    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    except ValueError:
        limit = 5

    results = search(query, limit)

    # Output JSON for programmatic use
    if os.environ.get("MEMORY_SEARCH_JSON"):
        print(json.dumps(results))
    else:
        print(format_results(results))


if __name__ == "__main__":
    # M-fix: the previous version had a fork that called search() with
    # sys.argv[1] without checking len(sys.argv) — crashed on no args.
    # Now: main() always handles its own arg validation and is the
    # single entry point.
    main()
