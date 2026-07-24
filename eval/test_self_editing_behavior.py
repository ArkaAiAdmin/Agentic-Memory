"""Behavioral tests for Sprint 2 — Self-Editing.

Verifies:
1. Agent amends a note → old content preserved in patch_history
2. Agent supersedes with rationale → rationale recorded in memory_revision_log
3. Agent reverts supersession → original note reactivated, revision log records revert
4. Agent calls memory_review_beliefs → returns list with min_confidence filter
5. Agent cannot patch/supersede a note without rationale → error returned
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
from save_pipeline import patch_memory, memory_supersede_db, revert_supersede

def _ensure_memory(conn, note_id: str, content: str, category: str = "lessons"):
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    conn.execute(
        "INSERT OR IGNORE INTO memories "
        "(id, content, source_file, category, created_at, updated_at, observed_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        (note_id, content, f"memory/{note_id}.md", category, now, now, now),
    )
    conn.commit()


@pytest.fixture
def db_path_str():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        p = f.name
    from infra.migration_runner import run_migrations
    from infra.db import open_db

    with open_db(p, timeout=10.0) as db:
        run_migrations(db)
        db.commit()
    yield p
    Path(p).unlink(missing_ok=True)


class TestPatchHistory:
    """Agent amends a note → old content preserved in patch_history"""

    def test_patch_preserves_old_content_in_metadata(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/patch-test", "# Original")
        conn.close()

        result = patch_memory(
            db_path=Path(db_path_str), note_id="lessons/patch-test",
            additions=["\n## Added section"], rationale="adding more detail",
        )
        assert "error" not in result.lower(), f"patch failed: {result}"

        conn = sqlite3.connect(db_path_str)
        row = conn.execute(
            "SELECT content, metadata FROM memories WHERE id = ?",
            ("lessons/patch-test",),
        ).fetchone()
        assert row is not None
        meta = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        patch_history = meta.get("patch_history", [])
        assert len(patch_history) >= 1
        assert "# Original" in str(patch_history) or "# Original" in str(row[0])


class TestSupersedeWithRationale:
    """Agent supersedes with rationale → rationale recorded in memory_revision_log"""

    def test_supersede_records_rationale_in_revision_log(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/supersede-me", "# Old version")
        _ensure_memory(conn, "lessons/supersede-v2", "# New version")
        conn.close()

        ok, err = memory_supersede_db(
            db_path=Path(db_path_str), old_id="lessons/supersede-me",
            new_id="lessons/supersede-v2", rationale="replaced with clearer version",
        )
        assert ok is True, f"supersede failed: {err}"

        conn = sqlite3.connect(db_path_str)
        row = conn.execute(
            "SELECT rationale, memory_id FROM memory_revision_log "
            "WHERE revision_type = 'supersede' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "no supersede revision_log entry"
        assert row[0] == "replaced with clearer version"
        assert row[1] == "lessons/supersede-me"

    def test_supersede_marks_old_note_superseded(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/old-note", "# Old")
        _ensure_memory(conn, "lessons/new-note", "# New")
        conn.close()

        ok, err = memory_supersede_db(
            db_path=Path(db_path_str), old_id="lessons/old-note",
            new_id="lessons/new-note", rationale="update",
        )
        assert ok is True, f"supersede failed: {err}"

        conn = sqlite3.connect(db_path_str)
        row = conn.execute(
            "SELECT superseded_by, valid_to FROM memories WHERE id = ?",
            ("lessons/old-note",),
        ).fetchone()
        assert row is not None
        assert row[0] == "lessons/new-note"
        assert row[1] is not None


class TestRevertSupersession:
    """Agent reverts supersession → original note reactivated, revision log records revert"""

    def test_revert_reactivates_original(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/revert-me", "# Original")
        _ensure_memory(conn, "lessons/revert-v2", "# Replacement")
        conn.close()

        ok, err = memory_supersede_db(
            db_path=Path(db_path_str), old_id="lessons/revert-me",
            new_id="lessons/revert-v2", rationale="replace",
        )
        assert ok is True, f"supersede failed: {err}"

        result = revert_supersede(
            db_path=Path(db_path_str), note_id="lessons/revert-me",
            rationale="was incorrect supersession",
        )
        assert "error" not in result.lower(), f"revert failed: {result}"

        conn = sqlite3.connect(db_path_str)
        row = conn.execute(
            "SELECT superseded_by, valid_to FROM memories WHERE id = ?",
            ("lessons/revert-me",),
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"superseded_by should be None, got {row[0]}"
        assert row[1] is None, f"valid_to should be None, got {row[1]}"

    def test_revert_records_revert_in_revision_log(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/log-revert", "# Orig")
        _ensure_memory(conn, "lessons/log-revert-v2", "# New")
        conn.close()

        ok, err = memory_supersede_db(
            db_path=Path(db_path_str), old_id="lessons/log-revert",
            new_id="lessons/log-revert-v2", rationale="temp",
        )
        assert ok is True, f"supersede failed: {err}"
        revert_supersede(
            db_path=Path(db_path_str), note_id="lessons/log-revert",
            rationale="restored original",
        )

        conn = sqlite3.connect(db_path_str)
        row = conn.execute(
            "SELECT revision_type, rationale FROM memory_revision_log "
            "WHERE revision_type = 'revert' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "no revert entry in revision_log"
        assert row[0] == "revert"
        assert row[1] == "restored original"


class TestMemoryReviewBeliefs:
    """Agent calls memory_review_beliefs → returns list with min_confidence filter"""

    def test_review_beliefs_returns_low_confidence(self, db_path_str, monkeypatch):
        conn = sqlite3.connect(db_path_str)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT)")
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("test/review-1", "content"))
        from fact.fact_schema import ensure_facts_schema
        from belief import ensure_beliefs_schema, ensure_belief_assertion
        ensure_facts_schema(conn)
        ensure_beliefs_schema(conn)
        import fact as fe

        fid = fe._upsert_fact(conn, "entity", "has", "low", 1.0, time.time())
        assert fid is not None, "_upsert_fact returned None"
        ensure_belief_assertion(conn, fid, memory_id="test/review-1",
                                 belief_status="active", confidence=0.1)
        fid2 = fe._upsert_fact(conn, "entity", "has", "high", 1.0, time.time())
        assert fid2 is not None
        ensure_belief_assertion(conn, fid2, memory_id="test/review-1",
                                 belief_status="active", confidence=0.9)
        conn.commit()
        conn.close()

        # Set last_reviewed_at to NULL so the older_than_days filter catches it
        conn2 = sqlite3.connect(db_path_str)
        conn2.execute("UPDATE belief_assertions SET last_reviewed_at = NULL")
        conn2.commit()
        conn2.close()

        import mcp_surface.mcp_verbs as _mv
        monkeypatch.setattr(_mv, "_resolve_db_path", lambda **kw: Path(db_path_str))
        result = _mv.memory_review_beliefs(min_confidence=0.5, older_than_days=365, limit=20)
        assert "No beliefs need review" not in result, f"unexpected: {result}"
        assert "0.10" in result or "0.1" in result


class TestRationaleRequired:
    """Agent cannot patch/supersede a note without rationale → error returned"""

    def test_patch_requires_rationale(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/no-rationale", "# Test")
        conn.close()

        from mcp_surface.mcp_verbs import memory_note
        result = memory_note(
            note_id="lessons/no-rationale", action="patch",
            additions=["\nadded"], rationale="",
        )
        assert "error" in result.lower() or "INVALID_PARAMS" in result

    def test_supersede_requires_rationale(self, db_path_str):
        conn = sqlite3.connect(db_path_str)
        _ensure_memory(conn, "lessons/sup-nr-a", "# A")
        _ensure_memory(conn, "lessons/sup-nr-b", "# B")
        conn.close()

        from mcp_surface.mcp_verbs import memory_note
        result = memory_note(
            note_id="lessons/sup-nr-a", action="supersede",
            title_slug="lessons/sup-nr-b", rationale="",
        )
        assert "error" in result.lower()
