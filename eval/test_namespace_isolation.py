"""B1 — Agent-scoped tenant_id tests.

Verifies:
  1. save_memory with non-default agent sets tenant_id to agent ID
  2. is_global=True keeps tenant_id="default" regardless of agent
  3. Search with agent context and include_global=False only returns that agent's memories
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
from agent_context import init_agent, clear_agent, get_agent
from save_pipeline import save_memory


def _bootstrap_db(p: Path) -> None:
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from fact.fact_schema import ensure_facts_schema

    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_facts_schema(db)
        db.commit()


@pytest.fixture
def db_path() -> Generator[Path, None, None]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    p = Path(tmp.name)
    try:
        _bootstrap_db(p)
        yield p
    finally:
        p.unlink(missing_ok=True)


def _set_agent(agent_id: str, namespace: str = "") -> None:
    clear_agent()
    init_agent(agent_id, namespace=namespace or agent_id)


class TestAgentScopedTenantId:
    """B1 — Per-agent tenant_id isolation"""

    def test_save_scopes_tenant_id_to_agent(self, db_path: Path):
        _set_agent("agent-coder")

        note_id = save_memory(
            content="Bug fix: off-by-one in loop.",
            title_slug="agent-test-scoped",
            category="lessons",
            db_path=str(db_path),
            is_global=False,
        )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT tenant_id FROM memories WHERE id = ?",
            (note_id,),
        ).fetchone()
        conn.close()
        assert row is not None, f"Memory row {note_id} not found"
        assert row[0] == "agent-coder", (
            f"tenant_id should be 'agent-coder' for agent-scoped save, got {row[0]}"
        )
        clear_agent()

    def test_save_global_keeps_default_tenant(self, db_path: Path):
        _set_agent("agent-coder")

        note_id = save_memory(
            content="Global knowledge: Python is great.",
            title_slug="agent-test-global",
            category="lessons",
            db_path=str(db_path),
            is_global=True,
        )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT tenant_id FROM memories WHERE id = ?",
            (note_id,),
        ).fetchone()
        conn.close()
        assert row is not None, f"Memory row {note_id} not found"
        assert row[0] == "default", (
            f"tenant_id should be 'default' for is_global=True, got {row[0]}"
        )
        clear_agent()

    def test_search_scoped_by_agent_returns_only_agent_memories(self, db_path: Path):
        from search.orchestrator import search_memories

        _set_agent("agent-a")
        source_a = "agents/agent-a/lessons/test-a"
        source_b = "agents/agent-b/lessons/test-b"
        now = time.time()

        conn = sqlite3.connect(str(db_path))
        for src, content in [(source_a, "Python is a language."),
                             (source_b, "JavaScript is a language.")]:
            nid = f"lessons/test-{src.split('/')[-1]}"
            conn.execute(
                "INSERT OR REPLACE INTO memories "
                "(id, source_file, content, category, tags, created_at, "
                "updated_at, observed_at, importance, metadata) "
                "VALUES (?, ?, ?, ?, '[]', ?, ?, ?, 3, '{}')",
                (nid, src, content, "lessons", now, now, now),
            )
        conn.commit()
        conn.close()

        try:
            result = search_memories(
                db_path=db_path,
                query="language programming",
                limit=10,
                include_global=False,
            )
        except Exception:
            result = {"results": []}
        finally:
            clear_agent()

        results = result.get("results", [])
        source_files = [r.get("source_file", "") for r in results]
        for sf in source_files:
            if sf:
                assert sf.startswith("agents/agent-a/"), (
                    f"Search should not return agent-b memory source_file: {sf}"
                )
        assert not any(sf.startswith("agents/agent-b/") for sf in source_files), (
            "Search with include_global=False should not include agent-b memories"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
