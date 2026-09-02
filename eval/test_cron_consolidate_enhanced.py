"""Test enhanced cron_consolidate deduplication and cleanup logic."""

import sqlite3
import os
import tempfile
import pytest
from pathlib import Path
from cron.cron_consolidate import (
    extract_clean_body,
    compute_content_hash,
    _find_exact_dups,
    _find_canonical_dups,
    _keep_id,
    consolidate,
)


def test_extract_clean_body_strips_frontmatter_and_title_hex():
    note1 = """---
created: 2026-08-04 15:07:07
updated: 2026-08-04 15:07:07
tags: [skill, doc]
superseded_by: null

# Skill Document Summarization Executive Insights F3Fc

SKILL: Document Summarization
Category: Documentation
Workflow: Step 1, Step 2.
"""

    note2 = """---
created: 2026-08-25 09:34:41
updated: 2026-08-25 09:34:41
tags: []
superseded_by: null

# Skill Document Summarization Executive Insights A6D2

SKILL: Document Summarization
Category: Documentation
Workflow: Step 1, Step 2.
"""

    body1 = extract_clean_body(note1)
    body2 = extract_clean_body(note2)
    assert body1 == body2
    assert "SKILL: Document Summarization" in body1
    assert "F3Fc" not in body1
    assert "2026-08-04" not in body1
    assert compute_content_hash(note1) == compute_content_hash(note2)


def test_find_canonical_dups_groups_templated_slugs():
    rows = [
        ("skill/builtin-doc-summarization", "content", "[]", "skill", 5, "2026-08-01"),
        ("skill/skill-document-summarization-executive-insights-1234", "content", "[]", "skill", 3, "2026-08-02"),
        ("projects/sub-agent-completed-navigate-to-hacker-news-1234", "content", "[]", "projects", 1, "2026-08-03"),
        ("projects/sub-agent-completed-navigate-to-hacker-news-5678", "content", "[]", "projects", 1, "2026-08-04"),
        ("lessons/tool-failure-lesson-1234", "Tool `runCommand` failed with exit 1", "[]", "lessons", 2, "2026-08-05"),
        ("lessons/tool-failure-lesson-5678", "Tool `runCommand` failed with exit 1", "[]", "lessons", 2, "2026-08-06"),
    ]

    groups = _find_canonical_dups(rows, existing_loser_ids=set())
    assert len(groups) >= 2
    proj_group = next(g for g in groups if "projects/sub-agent-completed-navigate-to-hacker-news-1234" in [g[0]] + g[1])
    assert "projects/sub-agent-completed-navigate-to-hacker-news-5678" in proj_group[1]


def test_consolidate_apply_merges_and_cleans_db(tmp_path):
    db_file = tmp_path / "test_memory.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            category TEXT NOT NULL,
            importance INTEGER DEFAULT 1,
            updated_at TEXT,
            deleted_at TEXT,
            deleted_by TEXT,
            superseded_by TEXT,
            tenant_id TEXT DEFAULT 'default'
        )
    """)
    conn.execute("CREATE TABLE memory_chunks (id INTEGER PRIMARY KEY, parent_id TEXT, content TEXT)")
    conn.execute("CREATE TABLE memory_chunk_embeddings (chunk_id INTEGER PRIMARY KEY, parent_id TEXT)")
    conn.execute("CREATE TABLE memory_embeddings (memory_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE memory_vec_keys (key INTEGER PRIMARY KEY, memory_id TEXT)")

    # Insert identical skill notes with different timestamps
    note1 = "---\ncreated: 2026-08-01\n\n# Skill Title A1\n\nSKILL: Refactoring\nBody text"
    note2 = "---\ncreated: 2026-08-02\n\n# Skill Title B2\n\nSKILL: Refactoring\nBody text"

    conn.execute("INSERT INTO memories (id, content, category, importance, updated_at) VALUES (?, ?, ?, ?, ?)",
                 ("skill/skill-refactoring-1111", note1, "skill", 1, "2026-08-01"))
    conn.execute("INSERT INTO memories (id, content, category, importance, updated_at) VALUES (?, ?, ?, ?, ?)",
                 ("skill/skill-refactoring-2222", note2, "skill", 4, "2026-08-02"))

    conn.execute("INSERT INTO memory_chunks (parent_id, content) VALUES ('skill/skill-refactoring-1111', 'chunk1')")
    conn.execute("INSERT INTO memory_embeddings (memory_id) VALUES ('skill/skill-refactoring-1111')")
    conn.execute("INSERT INTO memory_vec_keys (key, memory_id) VALUES (1, 'skill/skill-refactoring-1111')")
    conn.commit()
    conn.close()

    os.environ["MEMORY_DB_PATH"] = str(db_file)
    consolidate(dry_run=False)

    conn = sqlite3.connect(str(db_file))
    active_rows = conn.execute("SELECT id, superseded_by, deleted_at FROM memories WHERE deleted_at IS NULL").fetchall()
    deleted_rows = conn.execute("SELECT id, superseded_by, deleted_at FROM memories WHERE deleted_at IS NOT NULL").fetchall()

    assert len(active_rows) == 1
    # Survivor should be the one with higher importance (importance 4)
    assert active_rows[0][0] == "skill/skill-refactoring-2222"

    assert len(deleted_rows) == 1
    assert deleted_rows[0][0] == "skill/skill-refactoring-1111"
    assert deleted_rows[0][1] == "skill/skill-refactoring-2222"
    assert deleted_rows[0][2] is not None

    # Check that dependent chunks/embeddings for loser note were deleted
    remaining_chunks = conn.execute("SELECT count(*) FROM memory_chunks WHERE parent_id='skill/skill-refactoring-1111'").fetchone()[0]
    assert remaining_chunks == 0
    remaining_vecs = conn.execute("SELECT count(*) FROM memory_vec_keys WHERE memory_id='skill/skill-refactoring-1111'").fetchone()[0]
    assert remaining_vecs == 0

    conn.close()
