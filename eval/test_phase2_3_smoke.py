"""Phase 2+3 smoke tests — run via:
   AUTO_SAVE_TOOL_ALLOWLIST='*' MEMORY_SAGA_FALLBACK=allow
   pytest eval/test_phase2_3_smoke.py -v
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("AUTO_SAVE_TOOL_ALLOWLIST", "*")

import background.auto_save as _as
from background.tool_complete import _normalize_for_dedup, _tool_complete_inner


def make_db() -> tuple[Path, sqlite3.Connection, Path]:
    tmpdir = Path(tempfile.mkdtemp())
    db = tmpdir / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, source_file TEXT, content TEXT,
            tags TEXT, category TEXT, importance INTEGER DEFAULT 1,
            pinned INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
            observed_at TEXT, valid_from TEXT, valid_to TEXT,
            superseded_by TEXT, deleted_at TEXT, metadata TEXT,
            fitness_score REAL DEFAULT 0, importance_score REAL DEFAULT 0,
            tier TEXT DEFAULT 'warm', repo_id TEXT
        );
        CREATE TABLE IF NOT EXISTS user_access_log (
            note_id TEXT, access_ts REAL, source TEXT
        );
        CREATE TABLE IF NOT EXISTS file_mtimes (
            path TEXT PRIMARY KEY, mtime REAL, content_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mem_category ON memories(category);
        CREATE INDEX IF NOT EXISTS idx_ual_note ON user_access_log(note_id);
        """
    )
    conn.commit()
    return db, conn, tmpdir


def _fake_save_memory(conn_ref, *a, **kw):
    """Minimal save_memory replacement: writes to the active test connection."""
    cat = kw.get("category", "sessions")
    title_slug = kw.get("title_slug", kw.get("content", "")[:30])
    note_id_val = f"{cat}/{title_slug}"
    tags_list = kw.get("tags") or []
    now = kw.get("_now_iso") or "2026-07-03T10:00:00"
    imp = kw.get("importance", 1)
    conn_ref.execute(
        "INSERT OR REPLACE INTO memories (id, category, importance, tags, content, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (note_id_val, cat, imp, json.dumps(tags_list),
         kw.get("content", ""), now, now),
    )
    conn_ref.commit()
    return f"OK:{note_id_val}"


def test_sessions_default():
    db, conn, tmpdir = make_db()
    orig_db_path = _as.get_db_path
    _as.get_db_path = lambda: db
    try:
        with patch("infra._lazy_imports.save_memory",
                   side_effect=lambda *a, **kw: _fake_save_memory(conn, *a, **kw),
                   create=True):
            r = _tool_complete_inner(
                "Read", '{"path": "README.md"}', "result",
                "2026-07-03T10:00:00", category="sessions", importance=1, conn=conn,
            )
        assert r["saved"], f"Expected saved, got: {r}"
        assert "sessions/" in r["note_id"], f"note_id: {r['note_id']}"
        row = conn.execute(
            "SELECT category, importance, tags FROM memories WHERE id=?", (r["note_id"],)
        ).fetchone()
        assert row[0] == "sessions", f"category wrong: {row}"
        assert row[1] == 1, f"importance wrong: {row}"
        tags = json.loads(row[2])
        assert "auto-save" in tags, f"Missing auto-save: {tags}"
        assert "tool-log" in tags, f"Missing tool-log: {tags}"
        assert "auto-capture" not in tags, f"should not have auto-capture: {tags}"
        print("test_sessions_default: PASSED")
    finally:
        _as.get_db_path = orig_db_path
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_lessons_draft():
    db, conn, tmpdir = make_db()
    orig_db_path = _as.get_db_path
    _as.get_db_path = lambda: db
    try:
        with patch("infra._lazy_imports.save_memory",
                   side_effect=lambda *a, **kw: _fake_save_memory(conn, *a, **kw),
                   create=True):
            r = _tool_complete_inner(
                "Write", '{"path": "foo.py", "content": "x"}', "3 lines",
                "2026-07-03T10:00:01", category="lessons", importance=1,
                extra_tags=["auto-capture", "draft"], conn=conn,
            )
        assert r["saved"], f"Expected saved, got: {r}"
        assert "lessons/" in r["note_id"], f"note_id: {r['note_id']}"
        row = conn.execute(
            "SELECT category, importance, tags FROM memories WHERE id=?", (r["note_id"],)
        ).fetchone()
        assert row[0] == "lessons", f"category wrong: {row}"
        assert row[1] == 1, f"importance wrong: {row}"
        tags = json.loads(row[2])
        assert "auto-capture" in tags, f"Missing auto-capture: {tags}"
        assert "draft" in tags, f"Missing draft: {tags}"
        assert "tool-log" in tags, f"Missing tool-log: {tags}"
        print("test_lessons_draft: PASSED")
    finally:
        _as.get_db_path = orig_db_path
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_normalize_dedup():
    c1 = "Timestamp: 2026-07-03T10:00:00\nNote: same content"
    c2 = "Timestamp: 2026-07-03T10:00:01\nNote: same content"
    c3 = "UUID: 550e8400-e29b-41d4-a716-446655440000"
    n1 = _normalize_for_dedup(c1)
    n2 = _normalize_for_dedup(c2)
    n3 = _normalize_for_dedup(c3)
    assert n1 == n2, f"Timestamps not equalized: {n1!r} != {n2!r}"
    assert "2026" not in n1, f"Timestamp residue: {n1!r}"
    assert "e29b" not in n3
    print("test_normalize_dedup: PASSED")


def test_promote_drafts_dry_run():
    from cron.cron_promote_drafts import promote_drafts
    db, conn, tmpdir = make_db()
    try:
        conn.executescript("""
            INSERT INTO memories (id, category, importance, tags, metadata, content)
            VALUES
              ('lessons/draft-1', 'lessons', 1, '[\"auto-capture\",\"draft\"]', '{}', 'content1'),
              ('lessons/curated-1',  'lessons', 4, '[\"curated\"]', '{}', 'content2'),
              ('lessons/xsess-1',   'lessons', 2, '[\"cross-session\",\"auto-lesson\"]', '{}', 'content3');
            INSERT INTO user_access_log (note_id, access_ts, source)
            VALUES ('lessons/draft-1', strftime('%s','now'), 'search');
        """)
        conn.commit()
        result = promote_drafts(db, threshold=1, dry_run=True)
        assert result["scanned"] == 1, f"scanned wrong: {result}"
        assert len(result["promoted"]) == 1, f"expected 1 promoted: {result}"
        assert result["promoted"][0]["id"] == "lessons/draft-1"
        assert "would_promote" in result["promoted"][0]
        print("test_promote_drafts_dry_run: PASSED")
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_promote_drafts_writes():
    from cron.cron_promote_drafts import promote_drafts
    db, conn, tmpdir = make_db()
    try:
        conn.executescript("""
            INSERT INTO memories (id, category, importance, tags, metadata, content)
            VALUES
              ('lessons/to-promote', 'lessons', 1, '[\"auto-capture\",\"draft\"]', '{}', 'content');
            INSERT INTO user_access_log (note_id, access_ts, source)
            VALUES ('lessons/to-promote', strftime('%s','now'), 'search');
        """)
        conn.commit()
        result = promote_drafts(db, threshold=1, dry_run=False)
        assert result["scanned"] == 1
        assert len(result["promoted"]) == 1
        row = conn.execute(
            "SELECT importance, tags, metadata FROM memories WHERE id='lessons/to-promote'"
        ).fetchone()
        assert row[0] == 4, f"importance should be 4 after promote: {row[0]}"
        tags = json.loads(row[1])
        assert "promoted" in tags and "curated" in tags
        meta = json.loads(row[2])
        assert "promoted_at" in meta, f"promoted_at missing: {meta}"
        print("test_promote_drafts_writes: PASSED")
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_sessions_default()
    test_lessons_draft()
    test_normalize_dedup()
    test_promote_drafts_dry_run()
    test_promote_drafts_writes()
    print("\nAll Phase 2+3 smoke tests PASSED")
