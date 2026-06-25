#!/usr/bin/env python3
"""Streaming ingest example for the agentic_memory SDK.

Demonstrates a higher-throughput pattern: instead of saving one
note at a time (each going through the full saga), batch many
memories and use the SDK's `add()` method in a tight loop. The
saga handles the per-note plumbing (FTS5 + embeddings + chunks +
backlinks) without the caller having to manage it.

For real high-throughput scenarios, prefer `save_memory` with
`is_global=False` and a single batch.

Usage::

    /Users/arka/.config/agentic-memory/venv/bin/python examples/streaming_ingest.py
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


SAMPLE_NOTES = [
    "User works on agentic memory infrastructure.",
    "Project uses SQLite with FTS5 for full-text search.",
    "MCP server exposes 61 tools to clients.",
    "Vector search uses usearch + model2vec embeddings.",
    "Cross-encoder reranker improves result quality.",
    "Knowledge graph extracts entities from note text.",
    "Contradiction detector flags semantically opposing facts.",
    "Spaced repetition schedules reviews based on importance.",
    "Adaptive retention uses surprise-based forgetting curves.",
    "Multi-agent CRDT sync merges concurrent edits deterministically.",
    "Quality gates filter short and near-duplicate results.",
    "Background worker drains the task queue every 5 minutes.",
    "OkF export produces markdown + frontmatter for portability.",
    "Session start hook loads pinned notes into context.",
    "Proactive context hook suggests related memories mid-task.",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Streaming ingest demo",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=20,
        help="How many notes to ingest (default 20).",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle note text before saving (degrades quality but stress-tests the dedup path).",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Don't delete the temp DB after the run.",
    )
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="agentic_memory_stream_"))
    db_path = tmpdir / "memory.db"
    print(f"Using isolated DB: {db_path}")
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    try:
        from agentic_memory import Memory  # noqa: WPS433

        m = Memory(db_path=str(db_path))

        # ---- ingest phase ----
        notes = list(SAMPLE_NOTES)
        if args.shuffle:
            random.shuffle(notes)
        notes = (notes * ((args.count // len(notes)) + 1))[: args.count]

        t0 = time.perf_counter()
        ids: list[str] = []
        for i, text in enumerate(notes):
            tags = [f"batch-{i // 5}", "ingest"]
            note_id = m.add(text, tags=tags)
            ids.append(note_id)
        elapsed = time.perf_counter() - t0
        print(
            f"\n[1] Ingested {len(notes)} notes in {elapsed:.2f}s "
            f"({len(notes) / max(elapsed, 1e-6):.1f} notes/sec)"
        )

        # ---- search phase ----
        print("\n[2] Searching with a few queries...")
        for query in [
            "vector search",
            "MCP server",
            "knowledge graph",
        ]:
            t0 = time.perf_counter()
            results = m.search(query, limit=3)
            elapsed = time.perf_counter() - t0
            print(
                f"  q={query!r:30s}  hits={len(results):2d}  "
                f"time={elapsed * 1000:.1f}ms"
            )
            for r in results[:2]:
                print(f"      - {r.get('content', '')[:60]}")

        # ---- stats ----
        print("\n[3] Final stats:")
        print(f"  {m.stats()}")

        # ---- list ----
        listed = m.list(limit=5)
        print(f"\n[4] Recent ({len(listed)} shown of total):")
        for n in listed[:5]:
            print(f"  - {n.get('id', '?'):60s}  {n.get('content', '')[:40]}")

        print("\nDone. Streaming ingest path works.")
        return 0
    finally:
        if not args.keep_db:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"(cleaned up {tmpdir})")


if __name__ == "__main__":
    raise SystemExit(main())
