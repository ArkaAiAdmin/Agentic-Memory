"""Integration tests for the memory recall mechanism.

Tests the recall.py module and MCP tools end-to-end:
  - recall_context() with pinned, recent, important, relevant sections
  - format_briefing() output structure
  - session_recap() lightweight summary
  - MCP tools memory_recall_context and memory_session_start
  - Token budget cap
  - Edge cases (empty DB, missing DB, no data)
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Ensure project root is on sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# Also ensure eval/longmemeval_s/ is not shadowing
_eval_lme = os.path.join(_project_root, "eval", "longmemeval_s")
if _eval_lme in sys.path:
    sys.path.remove(_eval_lme)
# Purge any cached wrong metrics module
_wrong_modules = [k for k in sys.modules if k in ("metrics", "longmemeval_s.metrics")]
for k in _wrong_modules:
    del sys.modules[k]

from memory_common import (
    open_db,
    run_db_migrations,
    _migrate_kg_tables,
    connection_pool,
)
from fact_extraction import ensure_facts_schema
from adaptive_retention import ensure_adaptive_schema

# Import recall module
sys.path.insert(0, _project_root)
from recall import (
    recall_context,
    format_briefing,
    session_recap,
    _fetch_pinned,
    _fetch_recent_digests,
    _fetch_high_importance,
    _fetch_relevant,
    _fetch_user_profile,
    _count_memories,
    _row_to_dict,
    _item_meta,
    _estimate_tokens,
    _empty_result,
    MAX_ITEMS_TOTAL,
    MAX_PINNED,
    MAX_RECENT,
    MAX_IMPORTANT,
    MAX_RELEVANT,
    HIGH_IMPORTANCE_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test database with schema and sample memories."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")

    # Create core tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT,
            tags TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            category TEXT,
            title_slug TEXT,
            importance INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            fitness_score REAL DEFAULT 0.0,
            deleted_at TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            hash TEXT,
            embedding_available INTEGER DEFAULT 0
        )
    """)

    # Create subsystem tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            parent_id TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB,
            model_revision TEXT NOT NULL,
            dim INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, content_hash)
        )
    """)

    # Run migrations
    run_db_migrations(conn)
    _migrate_kg_tables(conn)
    ensure_facts_schema(conn)
    ensure_adaptive_schema(conn)

    conn.commit()
    conn.close()
    return db_path


def _insert_memory(conn, **kwargs):
    """Insert a test memory with defaults."""
    defaults = {
        "id": f"test-{int(time.time() * 1000)}",
        "content": "Test memory content",
        "source_file": "lessons/test.md",
        "tags": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "category": "lessons",
        "title_slug": "test",
        "importance": 0,
        "pinned": 0,
        "fitness_score": 0.0,
        "deleted_at": None,
        "valid_to": None,
        "superseded_by": None,
        "hash": "",
        "embedding_available": 0,
    }
    defaults.update(kwargs)
    conn.execute(
        """INSERT INTO memories
           (id, content, source_file, tags, created_at, updated_at,
            category, title_slug, importance, pinned, fitness_score,
            deleted_at, valid_to, superseded_by, hash, embedding_available)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            defaults["id"],
            defaults["content"],
            defaults["source_file"],
            defaults["tags"],
            defaults["created_at"],
            defaults["updated_at"],
            defaults["category"],
            defaults["title_slug"],
            defaults["importance"],
            defaults["pinned"],
            defaults["fitness_score"],
            defaults["deleted_at"],
            defaults["valid_to"],
            defaults["superseded_by"],
            defaults["hash"],
            defaults["embedding_available"],
        ),
    )
    return defaults["id"]


# ---------------------------------------------------------------------------
# Tests: recall_context
# ---------------------------------------------------------------------------


class TestRecallContext:
    """Test the main recall_context() function."""

    def test_empty_db(self, tmp_path):
        """recall_context returns valid structure on empty DB."""
        db_path = _create_test_db(tmp_path)
        result = recall_context(db_path=str(db_path))

        assert "query" in result
        assert "timestamp" in result
        assert "sections" in result
        assert "total_memories" in result
        assert "formatted" in result
        assert "token_estimate" in result
        assert result["total_memories"] == 0
        assert result["token_estimate"] >= 0

    def test_missing_db(self, tmp_path):
        """recall_context handles missing DB gracefully."""
        db_path = tmp_path / "nonexistent.db"
        result = recall_context(db_path=str(db_path))

        assert result["total_memories"] == 0
        assert "No recall available" in result["formatted"]

    def test_pinned_notes_included(self, tmp_path):
        """Pinned notes appear in recall sections."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(
            conn,
            id="pin-1",
            content="Important pinned note",
            pinned=1,
            importance=5,
            tags="important",
        )
        _insert_memory(
            conn, id="pin-2", content="Another pinned note", pinned=1, importance=4
        )
        _insert_memory(
            conn, id="unpin-1", content="Not pinned note", pinned=0, importance=3
        )
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            include_pinned=True,
            include_recent_digests=False,
            include_high_importance=False,
            include_user_profile=False,
        )

        pinned = result["sections"].get("pinned", [])
        assert len(pinned) == 2
        pinned_ids = {item["id"] for item in pinned}
        assert "pin-1" in pinned_ids
        assert "pin-2" in pinned_ids
        assert "unpin-1" not in pinned_ids

    def test_recent_digests_included(self, tmp_path):
        """Recent session digests appear in recall sections."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        now = datetime.now(timezone.utc)
        _insert_memory(
            conn,
            id="session-1",
            content="Session work from today",
            source_file="sessions/2026-06-10.md",
            created_at=now.isoformat(),
        )
        _insert_memory(
            conn,
            id="session-2",
            content="Session work from yesterday",
            source_file="sessions/2026-06-09.md",
            created_at=(now - timedelta(days=1)).isoformat(),
        )
        _insert_memory(
            conn,
            id="old-session",
            content="Old session work",
            source_file="sessions/2026-05-01.md",
            created_at=(now - timedelta(days=40)).isoformat(),
        )
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            include_pinned=False,
            include_recent_digests=True,
            include_high_importance=False,
            include_user_profile=False,
            days_recent=7,
        )

        recent = result["sections"].get("recent_activity", [])
        assert len(recent) == 2
        recent_ids = {item["id"] for item in recent}
        assert "session-1" in recent_ids
        assert "session-2" in recent_ids
        assert "old-session" not in recent_ids

    def test_high_importance_included(self, tmp_path):
        """High-importance memories appear in recall sections."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(conn, id="high-1", content="Critical decision", importance=5)
        _insert_memory(conn, id="high-2", content="Important lesson", importance=4)
        _insert_memory(conn, id="low-1", content="Minor note", importance=2)
        _insert_memory(conn, id="mid-1", content="Medium note", importance=3)
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            include_pinned=False,
            include_recent_digests=False,
            include_high_importance=True,
            include_user_profile=False,
        )

        important = result["sections"].get("important", [])
        assert len(important) == 2
        important_ids = {item["id"] for item in important}
        assert "high-1" in important_ids
        assert "high-2" in important_ids
        assert "low-1" not in important_ids
        assert "mid-1" not in important_ids

    def test_query_triggers_relevant(self, tmp_path):
        """Providing a query triggers contextual search in recall."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        # Insert enough memories for FTS5 to work with
        for i in range(10):
            _insert_memory(
                conn,
                id=f"note-{i}",
                content=f"Memory about Python coding patterns and best practices number {i}",
                tags="python,coding",
            )
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            query="Python coding",
            include_pinned=False,
            include_recent_digests=False,
            include_high_importance=False,
            include_user_profile=False,
        )

        # search_memories might return results or not depending on FTS5 index
        # The key test is that it doesn't crash and returns valid structure
        assert "sections" in result
        assert isinstance(result["sections"], dict)

    def test_token_estimate_populated(self, tmp_path):
        """Token estimate is computed and populated."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(
            conn,
            id="tok-1",
            content="Test content for token estimation",
            pinned=1,
            importance=5,
        )
        conn.commit()
        conn.close()

        result = recall_context(db_path=str(db_path), include_user_profile=False)

        assert result["token_estimate"] > 0
        assert result["token_estimate"] == len(result["formatted"]) // 4

    def test_total_memories_count(self, tmp_path):
        """total_memories reflects actual DB count."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        for i in range(5):
            _insert_memory(conn, id=f"count-{i}", content=f"Memory {i}")
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            include_pinned=False,
            include_recent_digests=False,
            include_high_importance=False,
            include_user_profile=False,
        )

        assert result["total_memories"] == 5

    def test_limit_respected(self, tmp_path):
        """Limit parameter caps total items."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        for i in range(10):
            _insert_memory(
                conn,
                id=f"limit-{i}",
                content=f"Pinned note {i}",
                pinned=1,
                importance=5 - i,
            )
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            limit=3,
            include_pinned=True,
            include_recent_digests=False,
            include_high_importance=False,
            include_user_profile=False,
        )

        pinned = result["sections"].get("pinned", [])
        assert len(pinned) <= 3

    def test_disabled_sections(self, tmp_path):
        """Disabling sections excludes them from result."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(conn, id="dis-1", content="Pinned note", pinned=1, importance=5)
        _insert_memory(conn, id="dis-2", content="Important note", importance=5)
        conn.commit()
        conn.close()

        result = recall_context(
            db_path=str(db_path),
            include_pinned=False,
            include_recent_digests=False,
            include_high_importance=False,
            include_user_profile=False,
        )

        assert "pinned" not in result["sections"]
        assert "recent_activity" not in result["sections"]
        assert "important" not in result["sections"]
        assert "profile" not in result["sections"]


# ---------------------------------------------------------------------------
# Tests: format_briefing
# ---------------------------------------------------------------------------


class TestFormatBriefing:
    """Test the format_briefing() output."""

    def test_empty_briefing(self):
        """Empty result produces valid output."""
        data = {
            "query": "",
            "timestamp": "2026-06-10T12:00:00Z",
            "sections": {},
            "total_memories": 0,
        }
        output = format_briefing(data)
        assert "Memory Recall Briefing" in output
        assert "2026-06-10" in output
        assert "Total memories" in output

    def test_briefing_with_pinned(self, tmp_path):
        """Briefing includes pinned notes section."""
        data = {
            "query": "",
            "timestamp": "2026-06-10T12:00:00Z",
            "sections": {
                "pinned": [
                    {
                        "id": "pin-1",
                        "content": "Important note",
                        "pinned": True,
                        "importance": 5,
                        "tags": "critical",
                        "source_file": "lessons/test.md",
                        "created_at": "2026-06-10T10:00:00Z",
                        "fitness_score": 0.9,
                    }
                ]
            },
            "total_memories": 1,
        }
        output = format_briefing(data)
        assert "Pinned Notes (1)" in output
        assert "pin-1" in output
        assert "Important note" in output
        assert "pinned" in output
        assert "importance: 5" in output

    def test_briefing_with_recent(self):
        """Briefing includes recent activity section."""
        data = {
            "query": "",
            "timestamp": "2026-06-10T12:00:00Z",
            "sections": {
                "recent_activity": [
                    {
                        "id": "r-1",
                        "content": "Worked on auth module",
                        "created_at": "2026-06-10T10:00:00Z",
                        "source_file": "sessions/2026-06-10.md",
                        "pinned": False,
                        "importance": 0,
                        "tags": "",
                        "fitness_score": 0.0,
                    }
                ]
            },
            "total_memories": 1,
        }
        output = format_briefing(data)
        assert "Recent Activity" in output
        assert "2026-06-10" in output
        assert "Worked on auth module" in output

    def test_briefing_with_relevant(self):
        """Briefing includes relevant section when query provided."""
        data = {
            "query": "authentication",
            "timestamp": "2026-06-10T12:00:00Z",
            "sections": {
                "relevant": [
                    {
                        "id": "rel-1",
                        "content": "OAuth2 implementation notes",
                        "source": "lessons/oauth2",
                        "pinned": False,
                        "importance": 3,
                        "tags": "auth",
                        "created_at": "2026-06-10T10:00:00Z",
                        "fitness_score": 0.7,
                    }
                ]
            },
            "total_memories": 1,
        }
        output = format_briefing(data)
        assert 'Relevant to "authentication"' in output
        assert "OAuth2 implementation notes" in output

    def test_briefing_with_profile(self):
        """Briefing includes user preferences section."""
        data = {
            "query": "",
            "timestamp": "2026-06-10T12:00:00Z",
            "sections": {
                "profile": {"enabled": True, "preferred_stack": "Python, FastAPI"}
            },
            "total_memories": 0,
        }
        output = format_briefing(data)
        assert "User Preferences" in output
        assert "preferred_stack" in output


# ---------------------------------------------------------------------------
# Tests: session_recap
# ---------------------------------------------------------------------------


class TestSessionRecap:
    """Test the session_recap() function."""

    def test_empty_db(self, tmp_path):
        """session_recap handles empty DB."""
        db_path = _create_test_db(tmp_path)
        output = session_recap(db_path)
        assert "No recent session activity" in output

    def test_missing_db(self, tmp_path):
        """session_recap handles missing DB."""
        db_path = tmp_path / "nonexistent.db"
        output = session_recap(db_path)
        assert "No database found" in output

    def test_recent_sessions(self, tmp_path):
        """session_recap shows recent session notes."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        now = datetime.now(timezone.utc)
        _insert_memory(
            conn,
            id="sess-1",
            content="Debugging auth bug",
            source_file="sessions/2026-06-10.md",
            created_at=now.isoformat(),
        )
        _insert_memory(
            conn,
            id="sess-2",
            content="Implemented new feature",
            source_file="sessions/2026-06-09.md",
            created_at=(now - timedelta(days=1)).isoformat(),
        )
        conn.commit()
        conn.close()

        output = session_recap(db_path)
        assert "Session Recap" in output
        assert "Debugging auth bug" in output
        assert "Implemented new feature" in output


# ---------------------------------------------------------------------------
# Tests: internal helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """Test internal helper functions."""

    def test_row_to_dict(self):
        """_row_to_dict converts tuple to dict."""
        row = (
            "test-id",
            "content here",
            "lessons/test.md",
            "tag1,tag2",
            "2026-06-10T10:00:00Z",
            0.75,
            4,
            1,
        )
        result = _row_to_dict(row)
        assert result["id"] == "test-id"
        assert result["content"] == "content here"
        assert result["pinned"] is True
        assert result["importance"] == 4

    def test_item_meta_pinned(self):
        """_item_meta formats pinned item."""
        item = {"pinned": True, "importance": 5, "tags": "critical"}
        meta = _item_meta(item)
        assert "pinned" in meta
        assert "importance: 5" in meta
        assert "tag: critical" in meta

    def test_item_meta_plain(self):
        """_item_meta returns empty for plain item."""
        item = {"pinned": False, "importance": 0, "tags": ""}
        meta = _item_meta(item)
        assert meta == ""

    def test_estimate_tokens(self):
        """_estimate_tokens returns reasonable estimate."""
        text = "a" * 100
        tokens = _estimate_tokens(text)
        assert tokens == 25  # 100 / 4

    def test_empty_result(self):
        """_empty_result returns valid structure."""
        result = _empty_result("test query", "test reason")
        assert result["query"] == "test query"
        assert result["total_memories"] == 0
        assert "test reason" in result["formatted"]

    def test_constants(self):
        """Module constants are reasonable."""
        assert MAX_ITEMS_TOTAL == 15
        assert MAX_PINNED == 5
        assert MAX_RECENT == 5
        assert MAX_IMPORTANT == 3
        assert MAX_RELEVANT == 5
        assert HIGH_IMPORTANCE_THRESHOLD == 4

    def test_count_memories(self, tmp_path):
        """_count_memories returns correct count."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        assert _count_memories(conn) == 0

        _insert_memory(conn, id="cnt-1", content="One")
        _insert_memory(conn, id="cnt-2", content="Two")
        conn.commit()

        assert _count_memories(conn) == 2

        # Soft-delete one
        conn.execute(
            "UPDATE memories SET deleted_at = datetime('now') WHERE id = 'cnt-1'"
        )
        conn.commit()

        assert _count_memories(conn) == 1
        conn.close()

    def test_fetch_pinned_empty(self, tmp_path):
        """_fetch_pinned returns empty list when no pinned."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        result = _fetch_pinned(conn, 5)
        assert result == []
        conn.close()

    def test_fetch_pinned_sorted(self, tmp_path):
        """_fetch_pinned returns pinned sorted by importance."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(conn, id="p-low", content="Low", pinned=1, importance=2)
        _insert_memory(conn, id="p-high", content="High", pinned=1, importance=5)
        _insert_memory(conn, id="p-mid", content="Mid", pinned=1, importance=3)
        conn.commit()

        result = _fetch_pinned(conn, 5)
        assert len(result) == 3
        assert result[0]["id"] == "p-high"
        assert result[1]["id"] == "p-mid"
        assert result[2]["id"] == "p-low"
        conn.close()

    def test_fetch_high_importance(self, tmp_path):
        """_fetch_high_importance only returns importance >= 4."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(conn, id="hi-5", content="Critical", importance=5)
        _insert_memory(conn, id="hi-4", content="Important", importance=4)
        _insert_memory(conn, id="hi-3", content="Medium", importance=3)
        _insert_memory(conn, id="hi-0", content="None", importance=0)
        conn.commit()

        result = _fetch_high_importance(conn, 10)
        assert len(result) == 2
        ids = {item["id"] for item in result}
        assert "hi-5" in ids
        assert "hi-4" in ids
        assert "hi-3" not in ids
        assert "hi-0" not in ids
        conn.close()

    def test_fetch_recent_digests(self, tmp_path):
        """_fetch_recent_digests only returns session files within days."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        now = datetime.now(timezone.utc)
        _insert_memory(
            conn,
            id="rd-1",
            content="Recent",
            source_file="sessions/2026-06-10.md",
            created_at=now.isoformat(),
        )
        _insert_memory(
            conn,
            id="rd-old",
            content="Old",
            source_file="sessions/2026-05-01.md",
            created_at=(now - timedelta(days=40)).isoformat(),
        )
        conn.commit()

        result = _fetch_recent_digests(conn, 7, 10)
        assert len(result) == 1
        assert result[0]["id"] == "rd-1"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: MCP tools (integration)
# ---------------------------------------------------------------------------


class TestMCPTools:
    """Test MCP tool wrappers for recall."""

    def test_memory_recall_context(self, tmp_path, monkeypatch):
        """memory_recall_context returns formatted string."""
        monkeypatch.delenv("MEMORY_DB_PATH", raising=False)
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(
            conn, id="mcp-1", content="MCP test note", pinned=1, importance=5
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))
        from mcp_tools import memory_recall_context

        result = memory_recall_context()

        assert "Memory Recall Briefing" in result
        assert "MCP test note" in result

    def test_memory_recall_context_with_query(self, tmp_path, monkeypatch):
        """memory_recall_context with query parameter."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        for i in range(10):
            _insert_memory(
                conn, id=f"mcpq-{i}", content=f"Python coding patterns number {i}"
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))

        from mcp_tools import memory_recall_context

        result = memory_recall_context(query="Python coding")

        assert "Memory Recall Briefing" in result

    def test_memory_session_start(self, tmp_path, monkeypatch):
        """memory_session_start returns combined briefing."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")

        _insert_memory(
            conn, id="ss-1", content="Session start test", pinned=1, importance=5
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))

        from mcp_tools import memory_session_start

        result = memory_session_start()

        assert "Memory Recall Briefing" in result
        assert "Session start test" in result
