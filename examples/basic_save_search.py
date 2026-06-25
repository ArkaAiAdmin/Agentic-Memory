#!/usr/bin/env python3
"""Basic save/search example for the agentic_memory SDK.

Demonstrates the minimal Mem0-compatible API:

    from agentic_memory import Memory

    m = Memory()
    m.add("...")
    results = m.search("...")

Usage::

    python examples/basic_save_search.py

The script isolates itself with a per-run temp DB so it doesn't pollute
the global memory store. Pass --keep-db to retain the DB after exit.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# When running this file directly, make the package importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Basic agentic_memory save+search demo",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Don't delete the temp DB after the run.",
    )
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="agentic_memory_basic_"))
    db_path = tmpdir / "memory.db"
    print(f"Using isolated DB: {db_path}")
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    try:
        from agentic_memory import Memory  # noqa: WPS433 — lazy import for test isolation

        m = Memory(db_path=str(db_path))

        print("\n[1] Saving three memories...")
        for text, tags in [
            ("User prefers dark mode in all editors.", ["preferences", "ui"]),
            ("User is learning Rust and building a CLI tool.", ["learning", "rust"]),
            ("Project uses PostgreSQL with the pgvector extension.", ["database"]),
        ]:
            note_id = m.add(text, tags=tags)
            print(f"  + {note_id:60s} <- {text!r}")

        print("\n[2] Searching for 'preferences'...")
        results = m.search("preferences", limit=3)
        for r in results:
            print(f"  - {r.get('id', '?'):60s}  score={r.get('score', 0):.3f}")
            print(f"      {r.get('content', '')[:80]}")

        print("\n[3] Listing recent memories...")
        notes = m.list(limit=5)
        print(f"  total: {len(notes)}")
        for n in notes[:3]:
            print(f"  - {n.get('id', '?')}")

        print("\n[4] Stats:")
        print(f"  {m.stats()}")

        print("\nDone. SDK is working end-to-end.")
        return 0
    finally:
        if not args.keep_db:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"(cleaned up {tmpdir})")


if __name__ == "__main__":
    raise SystemExit(main())
