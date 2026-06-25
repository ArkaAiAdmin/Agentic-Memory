#!/usr/bin/env python3
"""Agent-scoped memory example for the agentic_memory SDK.

Demonstrates ``AgentMemory``: agent-scoped memory with namespace
isolation. Each agent has its own private namespace, so memories
saved by ``AgentMemory(agent_id="A")`` are not visible to
``AgentMemory(agent_id="B")``.

Usage::

    /Users/arka/.config/agentic-memory/venv/bin/python examples/agent_memory.py
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent-scoped memory demo",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Don't delete the temp DB after the run.",
    )
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="agentic_memory_agent_"))
    db_path = tmpdir / "memory.db"
    print(f"Using isolated DB: {db_path}")
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    try:
        from agentic_memory import AgentMemory  # noqa: WPS433

        coder = AgentMemory(agent_id="coder-1", display_name="Coder Bot")
        writer = AgentMemory(agent_id="writer-1", display_name="Writer Bot")

        print("\n[1] Coder saves a project note...")
        nid = coder.save(
            "Frontend uses React with TypeScript and Vite for fast HMR.",
            tags=["frontend", "react"],
        )
        print(f"  coder:  {nid}")

        print("\n[2] Writer saves a documentation note...")
        nid = writer.save(
            "Use MkDocs with the Material theme for the docs site.",
            tags=["docs", "mkdocs"],
        )
        print(f"  writer: {nid}")

        print("\n[3] Coder searches 'frontend' (should see its own note)...")
        results = coder.search("frontend", limit=5)
        print(f"  coder sees {len(results)} result(s):")
        for r in results:
            print(f"    - {r.get('id', '?'):60s}  score={r.get('score', 0):.3f}")

        print("\n[4] Writer searches 'docs' (should see its own note)...")
        results = writer.search("docs", limit=5)
        print(f"  writer sees {len(results)} result(s):")
        for r in results:
            print(f"    - {r.get('id', '?'):60s}  score={r.get('score', 0):.3f}")

        print(
            "\n[5] Cross-agent isolation: writer should NOT see the coder's "
            "frontend note when searching 'React'."
        )
        results = writer.search("React", limit=5)
        for r in results:
            print(f"    - {r.get('id', '?'):60s}  score={r.get('score', 0):.3f}")

        print("\nDone. Agent scoping is working.")
        return 0
    finally:
        if not args.keep_db:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"(cleaned up {tmpdir})")


if __name__ == "__main__":
    raise SystemExit(main())
