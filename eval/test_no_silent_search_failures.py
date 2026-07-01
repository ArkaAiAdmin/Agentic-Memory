"""
Regression test: search_memories must never silently return an "Error [...]" string.

The bug this guards against: in 2026-06, the live search silently returned
"Error [DB_ERROR]: Search failed: fts5: syntax error near \"/\"" for any query
that triggered KG expansion, because _parse_search_query built an FTS5 query
with raw KG entity names (some had embedded / and quotes). The error was
buried in the output string; the count was 0; the call appeared to succeed.

This test would have caught it. It runs the public API against a temp DB seeded
with bad data, and asserts the output is never an error string.

See: projects/active-tutorial-hardening-2026-06-16
"""

import pytest
import sqlite3
from pathlib import Path

from memory_mcp import search_memories
from infra.memory_common import get_memory_paths


# Snapshot of high-traffic queries that MUST always work, derived from the
# opencode proactive-context hook's common triggers
QUERIES_THAT_MUST_NOT_FAIL = [
    "memory",
    "agentic",
    "sqlite",
    "rag",
    "hooks",
    "crdt",
    "h1 hook",
    "schema migration",
    "kg_dedup",
    "reranker",
    "fts5",
    "embedding",
    "session start",
    "compaction",
    "context monitor",
    "contradiction",
    "tier migration",
    "user profile",
    "spaced repetition",
    "cross agent",
]


def _bootstrap_temp_db(db_path: Path) -> None:
    """Create a fully-bootstrapped temp DB by copying the live schema.

    We can't easily run the full migration from scratch (some tables are
    created on first-use), so we copy the schema from the live prod DB
    via sqlite3 backup, then add our seed data.
    """
    import shutil

    _, _, global_mem = get_memory_paths()
    prod_db = global_mem / "memory.db"
    if prod_db.exists():
        shutil.copy2(prod_db, db_path)


def _seed_minimal_memory(c: sqlite3.Connection) -> None:
    """Insert one memory + matching FTS5 row so search has something to find."""
    c.execute("""
        INSERT OR REPLACE INTO memories (
            id, content, category, source_file,
            created_at, updated_at, observed_at
        ) VALUES (
            'lessons/test-no-silent-001',
            'agentic memory is a local-first SQLite system',
            'lessons',
            'memory/lessons/test-no-silent-001.md',
            '2026-06-15 00:00:00',
            '2026-06-15 00:00:00',
            '2026-06-15 00:00:00'
        )
    """)
    rowid = c.execute(
        "SELECT rowid FROM memories WHERE id='lessons/test-no-silent-001'"
    ).fetchone()[0]
    c.execute(
        """
        INSERT OR REPLACE INTO memories_fts (rowid, content, tags)
        VALUES (?, 'agentic memory is a local-first SQLite system', 'lessons')
    """,
        (rowid,),
    )
    c.commit()


def _seed_bad_kg_entity(c: sqlite3.Connection) -> None:
    """Insert a malformed KG entity (the kind that triggered the 2026-06
    FTS5 syntax error: embedded / and embedded " in a bash command)."""
    c.execute("""
        INSERT OR REPLACE INTO kg_entities (id, name, entity_type, mentions, created_at, updated_at)
        VALUES (1, 'json { "command": "cd /users/arka/.config/agentic-memory && venv/bin/python -c \\"import json\\"" }',
                'concept', 1, '2026-06-15 00:00:00', '2026-06-15 00:00:00')
    """)
    c.commit()


@pytest.fixture
def temp_db_clean(tmp_path):
    db_path = tmp_path / "memory.db"
    _bootstrap_temp_db(db_path)
    c = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        _seed_minimal_memory(c)
    finally:
        c.close()
    return db_path


@pytest.fixture
def temp_db_with_bad_data(tmp_path):
    db_path = tmp_path / "memory.db"
    _bootstrap_temp_db(db_path)
    c = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        _seed_minimal_memory(c)
        _seed_bad_kg_entity(c)
    finally:
        c.close()
    return db_path


class TestNoSilentSearchFailures:
    """Every public search must return a non-error string, even on
    data-state combinations that triggered the 2026-06 FTS5 bug."""

    def test_search_on_clean_db_never_returns_error(self, temp_db_clean):
        for q in QUERIES_THAT_MUST_NOT_FAIL:
            r = search_memories(temp_db_clean, q, limit=3)
            out = r.get("output", "")
            assert not out.startswith("Error ["), (
                f"search('{q}') on clean DB returned error: {out[:200]}"
            )

    def test_search_on_db_with_bad_kg_entity_never_returns_error(
        self, temp_db_with_bad_data
    ):
        """This is the regression test for the 2026-06 FTS5 bug.

        A query that triggers KG expansion must not return an error string
        even when a malformed entity exists in the KG."""
        for q in QUERIES_THAT_MUST_NOT_FAIL:
            r = search_memories(temp_db_with_bad_data, q, limit=3)
            out = r.get("output", "")
            assert not out.startswith("Error ["), (
                f"search('{q}') on DB with bad KG entity returned error: {out[:200]}"
            )

    def test_search_with_special_chars_in_query(self, temp_db_clean):
        """Edge case: queries containing characters that are FTS5 special."""
        for q in ['a"b', "a'b", "a/b", "a*b", "a^b", "a:b"]:
            r = search_memories(temp_db_clean, q, limit=2)
            out = r.get("output", "")
            assert not out.startswith("Error ["), (
                f"search('{q}') returned error: {out[:200]}"
            )

    def test_search_with_unicode_queries(self, temp_db_clean):
        for q in ["café résumé", "日本語", "🚀 emoji", "Ω∑"]:
            r = search_memories(temp_db_clean, q, limit=2)
            out = r.get("output", "")
            assert not out.startswith("Error ["), (
                f"search('{q}') returned error: {out[:200]}"
            )

    def test_search_returns_dict_with_required_keys(self, temp_db_clean):
        """The public API must return a dict with the expected shape, not a string."""
        r = search_memories(temp_db_clean, "memory", limit=3)
        assert isinstance(r, dict), f"expected dict, got {type(r).__name__}"
        assert "results" in r, f"missing 'results' key: {list(r.keys())}"
        assert "count" in r, f"missing 'count' key: {list(r.keys())}"
        assert "output" in r, f"missing 'output' key: {list(r.keys())}"
        assert isinstance(r["results"], list), "'results' is not a list"
        assert isinstance(r["count"], int), "'count' is not an int"
        assert isinstance(r["output"], str), "'output' is not a str"


class TestSearchResilience:
    """Specific scenarios that have broken in the past."""

    def test_search_with_kg_term_containing_slash(self, temp_db_with_bad_data):
        """The exact failure mode from 2026-06-16: a KG entity with '/'
        breaks FTS5 query construction. Must now be safe."""
        r = search_memories(temp_db_with_bad_data, "memory", limit=3)
        out = r.get("output", "")
        # Before fix: "Error [DB_ERROR]: Search failed: fts5: syntax error near \"/\""
        # After fix: "Search results for: 'memory' (Re-ranked)..."
        assert not out.startswith("Error ["), out[:200]

    def test_search_with_kg_term_containing_embedded_quotes(
        self, temp_db_with_bad_data
    ):
        """The other half of the 2026-06-16 bug: a KG entity with
        embedded double-quotes broke FTS5 phrase quoting."""
        r = search_memories(temp_db_with_bad_data, "agentic", limit=3)
        out = r.get("output", "")
        assert not out.startswith("Error ["), out[:200]

    def test_search_finds_known_memory_even_with_bad_kg(self, temp_db_with_bad_data):
        """End-to-end: with bad KG data present, search for a known
        note should still return it. (The 2026-06 bug returned 0 results.)"""
        r = search_memories(
            temp_db_with_bad_data, "agentic memory local-first SQLite", limit=3
        )
        count = r.get("count", 0)
        assert count > 0, (
            f"expected to find the test memory, got count={count}, output={r.get('output', '')[:200]}"
        )
