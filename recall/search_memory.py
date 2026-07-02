#!/usr/bin/env python3
"""CLI search wrapper. Thin wrapper over the canonical search pipeline.

The FTS5 query construction, scoring, and result formatting all live in
``search.orchestrator.search_memories`` (re-exported as
``search_pipeline.search_memories``) now. This module just resolves the
local + global DB paths and prints the formatted output for the CLI /
agent_init.

This is the H7 refactor: one source of truth for retrieval. Fixes to the
FTS5 query (e.g. the C1 implicit-AND → OR change) now apply everywhere
automatically, instead of being duplicated and drifting in two files.

B1 fix (2026-06-22): import the canonical ``search_memories`` from
``search_pipeline`` (which itself is a re-export of
``search.orchestrator.search_memories``) instead of going through
``memory_mcp``. ``memory_mcp`` only re-exports the symbol and is on
itself a thin alias of the canonical, so the import chain now reads
``search_memory → search_pipeline → search.orchestrator`` with no
intermediate re-implementations.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".config" / "agentic-memory"))
from infra.memory_config import get_memory_paths  # noqa: E402
from search_pipeline import search_memories as _mcp_search_memories  # noqa: E402


def _resolve_db_paths(custom_db_path=None):
    """Return (db_file, global_db) — the local + global memory DBs.

    Mirrors the path logic that previously lived in this file, so the
    public search_memories() signature keeps working for callers like
    agent_init.py and the __main__ CLI.
    """
    _, local_mem, _ = get_memory_paths()
    if custom_db_path:
        db_file = Path(custom_db_path)
    else:
        db_file = local_mem / "memory.db"
    from infra._lazy_imports import GLOBAL_MEM_DIR

    local_global_link = local_mem / "global"
    if local_global_link.is_symlink():
        global_db = local_global_link.resolve() / "memory.db"
    else:
        global_db = GLOBAL_MEM_DIR / "memory.db"
    return db_file, global_db


def search_memories(
    query,
    limit=5,
    custom_db_path=None,
    include_global=True,
    min_local_results=3,
    silent=False,
    include_invalid=True,
):
    """Search local (+ optionally global) memory and print formatted results.

    Thin wrapper around memory_mcp.search_memories. We delegate the FTS5
    query construction, scoring, rerank, and result formatting to
    memory_mcp so there is one source of truth for retrieval.

    Returns a list of result-item dicts (memory_mcp's format) so the
    shape matches memory_mcp.search_memories' 'results' field. Callers
    that only care about the printed output can ignore the return.
    """
    db_file, global_db = _resolve_db_paths(custom_db_path=custom_db_path)

    if not db_file.exists():
        if not silent:
            print(
                f"Error: Memory database {db_file} does not exist. "
                "Run rebuild_index.py first."
            )
        return []

    all_items = []
    sources_searched = []

    local_result = _mcp_search_memories(
        db_file,
        query,
        limit=limit,
        include_global=False,
        rerank=True,
        boost_pinned=True,
        include_invalid=include_invalid,
    )
    if local_result.get("results"):
        all_items.extend(_tag(local_result["results"], "local"))
        sources_searched.append("local")

    if (
        include_global
        and global_db.exists()
        and len(local_result.get("results", [])) < min_local_results
    ):
        global_result = _mcp_search_memories(
            global_db,
            query,
            limit=limit,
            include_global=False,
            rerank=True,
            boost_pinned=True,
            include_invalid=include_invalid,
        )
        if global_result.get("results"):
            all_items.extend(_tag(global_result["results"], "global"))
            sources_searched.append("global")

    # Pretty print in the legacy format so existing CLI output is preserved.
    source_str = " + ".join(sources_searched) if sources_searched else "none"
    print(f"\nSearch results for: '{query}' (Top {len(all_items)} from {source_str})")
    print("=" * 80)
    seen_ids = set()
    for i, item in enumerate(all_items, 1):
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        tags = item.get("tags", [])
        tags_str = ", ".join(tags) if tags else "none"
        source_label = f"[{item.get('source_db', '')}]" if item.get("source_db") else ""
        score = item.get("final_score", 0.0)
        print(f"[{i}] {item['id']} (Score: {score:.2f}) {source_label}")
        print(f"    Source: memory/{item['source_file']}")
        print(f"    Tags: {tags_str}")
        print(f"    Created: {item.get('created', '')}")
        print(f"    Content:\n    {item.get('content', '').strip()}")
        print()
    print("=" * 80)

    # Increment access_count for every displayed result (legacy side effect).
    note_ids = [item["id"] for item in all_items if item.get("id")]
    dbs_to_update = [db_file]
    if include_global and "global" in sources_searched:
        dbs_to_update.append(global_db)
    for db_path in dbs_to_update:
        if not db_path.exists() or not note_ids:
            continue
        try:
            from infra._lazy_imports import open_db

            placeholders = ",".join("?" * len(note_ids))
            with open_db(db_path) as conn:
                conn.execute(
                    f"UPDATE memories SET access_count = access_count + 1 "
                    f"WHERE id IN ({placeholders})",
                    note_ids,
                )
                conn.commit()
        except Exception:
            pass

    return all_items


def _tag(items, source_db):
    """Attach a 'source_db' label to each result item dict."""
    out = []
    for it in items:
        copy = dict(it)
        copy["source_db"] = source_db
        out.append(copy)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: search_memory.py <query> [limit] [--no-global] [db_path]")
        sys.exit(1)
    query = sys.argv[1]
    limit = 5
    include_global = True
    db_path = None
    for arg in sys.argv[2:]:
        if arg == "--no-global":
            include_global = False
        elif not arg.startswith("--"):
            if arg.isdigit():
                limit = int(arg)
            else:
                db_path = arg
    search_memories(
        query,
        limit=limit,
        custom_db_path=db_path,
        include_global=include_global,
        silent=False,
    )
