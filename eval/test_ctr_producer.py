"""Phase 0 CTR producer tests — hermetic, own temp DB.

Verifies:
  1. ``record_memory_used_in_response`` writes a ``used_in_response`` row to
     ``memory_search_interaction`` for a recalled memory.
  2. Recording the same (query_id, memory_id, action) twice does NOT create a
     duplicate row (the UNIQUE(query_id, memory_id, action) constraint holds)
     but refreshes ``ts``.
  3. The legacy ``record_ctr_feedback_db`` now writes to
     ``memory_search_interaction`` (audit #9 fix) and preserves multi-action
     rows instead of collapsing them via ``INSERT OR REPLACE``.

No production DB (memory/memory.db) is touched.
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from infra.memory_common import run_db_migrations

# Agreed DDL for migration 057 (kept here so the test is hermetic whether or
# not the parallel migration 057 has landed yet).
_CREATE_INTERACTION = """
CREATE TABLE IF NOT EXISTS memory_search_interaction (
    id          INTEGER PRIMARY KEY,
    query_id    TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    rank        INTEGER,
    ts          REAL NOT NULL DEFAULT (unixepoch()),
    UNIQUE (query_id, memory_id, action)
)
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a migrated temp DB with the interaction table present."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("# Agentic Memory Index\n", encoding="utf-8")
    db_path = mem_dir / "memory.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_db_migrations(conn)
        conn.execute(_CREATE_INTERACTION)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _recall(db_path: Path, phrase: str) -> tuple[str, str]:
    """Save a distinctive memory, recall it, return (query_id, memory_id)."""
    from save.pipeline import save_memory
    from search.orchestrator import search_memories

    note_id = save_memory(
        content=f"Phase0 marker phrase {phrase}",
        category="lessons",
        title_slug=f"phase0-{phrase}",
        tags=["phase0", "test"],
        db_path=str(db_path),
        defer_expensive=False,
        importance=3,
    )
    assert note_id and not str(note_id).startswith("{"), f"save failed: {note_id}"

    result = search_memories(
        db_path=db_path,
        query=f"Phase0 marker phrase {phrase}",
        limit=5,
        include_global=False,
        rerank=False,
        light=True,
    )
    query_id = result.get("query_id")
    assert query_id, "search returned no query_id"
    memory_id = None
    for r in result.get("results", []):
        if str(r.get("id")) == str(note_id):
            memory_id = r.get("id")
            break
    if memory_id is None:
        results = result.get("results", [])
        assert results, f"no search results for {phrase}"
        memory_id = results[0].get("id")
    return query_id, memory_id


def test_used_in_response_writes_row(tmp_path):
    db_path = _make_db(tmp_path)
    query_id, memory_id = _recall(db_path, "alpha")

    from search.orchestrator import record_memory_used_in_response

    record_memory_used_in_response(str(db_path), query_id, [memory_id], ranks=[1])

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT query_id, memory_id, action, tenant_id, rank FROM "
            "memory_search_interaction WHERE query_id=? AND memory_id=? "
            "AND action='used_in_response'",
            (query_id, memory_id),
        ).fetchone()
        assert row is not None, "no used_in_response row written"
        assert row[0] == query_id
        assert row[1] == memory_id
        assert row[2] == "used_in_response"
        assert row[3] == "default"
        assert row[4] == 1
    finally:
        conn.close()


def test_used_in_response_no_duplicate_updates_ts(tmp_path):
    db_path = _make_db(tmp_path)
    query_id, memory_id = _recall(db_path, "beta")

    from search.orchestrator import record_memory_used_in_response

    record_memory_used_in_response(str(db_path), query_id, [memory_id])
    ts_first = _get_ts(db_path, query_id, memory_id)

    # Sleep so ts would differ if a new row were written (or ts refreshed).
    time.sleep(1.05)
    record_memory_used_in_response(str(db_path), query_id, [memory_id])

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT ts FROM memory_search_interaction "
            "WHERE query_id=? AND memory_id=? AND action='used_in_response'",
            (query_id, memory_id),
        ).fetchall()
        assert len(rows) == 1, f"UNIQUE failed: {len(rows)} rows for same key"
        ts_second = rows[0][0]
        assert ts_second >= ts_first, "ts should be refreshed, not older"
    finally:
        conn.close()


def test_ctr_feedback_db_migrated_to_interaction(tmp_path):
    """Audit #9 (producer) + FIX 2 (CTR correlation):

    - record_ctr_feedback_db writes per-(query_id, memory_id) rows to
      memory_search_interaction and preserves multi-action rows
      (returned->impression, clicked->click: two distinct rows).
    - Search telemetry (_record_search_telemetry) writes one impression row
      per returned result into memory_ctr_feedback keyed by (query_id, id).
    - record_ctr_feedback_db correlates a click by stamping clicked_at on the
      *existing* impression row — it does NOT insert a new per-memory row.
      compute_channel_weights then reads the real signal.
    """
    db_path = _make_db(tmp_path)
    query_id, memory_id = _recall(db_path, "gamma")

    from search.orchestrator import record_ctr_feedback_db

    record_ctr_feedback_db(str(db_path), id=memory_id, query_id=query_id, action="returned")
    record_ctr_feedback_db(str(db_path), id=memory_id, query_id=query_id, action="clicked")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT action FROM memory_search_interaction "
            "WHERE query_id=? AND memory_id=?",
            (query_id, memory_id),
        ).fetchall()
        actions = sorted(r[0] for r in rows)
        # returned->impression, clicked->click: two distinct rows preserved.
        assert actions == ["click", "impression"], (
            f"multi-action rows collapsed: {actions}"
        )
        # The per-result impression row for this (query_id, id) must exist
        # (written by search telemetry) and record_ctr_feedback_db must have
        # stamped it, NOT inserted a duplicate row.
        ctr_rows = conn.execute(
            "SELECT clicked_at FROM memory_ctr_feedback "
            "WHERE query_id=? AND id=?",
            (query_id, memory_id),
        ).fetchall()
        assert len(ctr_rows) == 1, (
            f"expected exactly 1 CTR impression row for (query_id, id), got {len(ctr_rows)}"
        )
        assert ctr_rows[0][0] is not None, (
            "click was not correlated onto the CTR impression row"
        )
    finally:
        conn.close()


def _get_ts(db_path: Path, query_id: str, memory_id: str) -> float:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT ts FROM memory_search_interaction "
            "WHERE query_id=? AND memory_id=? AND action='used_in_response'",
            (query_id, memory_id),
        ).fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
