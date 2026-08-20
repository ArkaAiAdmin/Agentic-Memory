"""Unit tests for Sleep-Time Memory Consolidation Pipeline (Phase 3)."""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from consolidation import compact_episodic_traces, detect_duplicates, merge_suggestions
from infra.migration_runner import run_migrations


@pytest.fixture
def episodic_db(tmp_path: Path):
    db_path = tmp_path / 'test_consolidation.db'
    conn = sqlite3.connect(str(db_path))
    run_migrations(conn)

    # Insert 6 granular episodic steps for trajectory traj_1234abcd
    cur = conn.cursor()
    steps = [
        ("sessions/traj_1234abcd.md", "[Trajectory 1234abcd] Goal: Configure OAuth2 authentication provider. [STATUS: RUNNING]", "sessions"),
        ("steps/traj_1234abcd_s1.md", "[1234abcd] Step 1 Observation: Opened auth settings page at /admin/auth", "steps"),
        ("steps/traj_1234abcd_s2.md", "[1234abcd] Step 2 Observation: Selected Google OAuth2 provider option", "steps"),
        ("steps/traj_1234abcd_s3.md", "[1234abcd] Step 3 Observation: Client secret was initially rejected due to whitespace", "steps"),
        ("steps/traj_1234abcd_s4.md", "[1234abcd] Step 4 Observation: Stripped whitespace and entered valid credentials", "steps"),
        ("steps/traj_1234abcd_s5.md", "[1234abcd] Step 5 Observation: Test login succeeded. [STATUS: SUCCESS]", "steps"),
    ]
    for src_file, content, cat in steps:
        slug = src_file.split('/')[-1].replace('.md', '')
        cur.execute(
            """INSERT INTO memories (id, source_file, content, category, importance, created_at, updated_at, observed_at)
               VALUES (?, ?, ?, ?, 3, '2026-08-20T10:00:00Z', '2026-08-20T10:00:00Z', '2026-08-20T10:00:00Z')""",
            (f"mem_{slug}", src_file, content, cat)
        )
    conn.commit()

    yield conn
    conn.close()


def test_compact_episodic_traces_synthesis(episodic_db):
    """Verify that 6 granular steps are distilled into 1 structured semantic note with provenance."""
    results = compact_episodic_traces(episodic_db, min_steps=4, save_to_db=False)
    assert len(results) == 1

    entry = results[0]
    assert entry["session_key"] == "traj_1234abcd"
    assert entry["goal"] == "Configure OAuth2 authentication provider."
    assert entry["status"] == "SUCCESS"
    assert entry["step_count"] == 6
    assert len(entry["derived_from_ids"]) == 6
    assert "[Consolidated Session traj_1234abcd]" in entry["content"]
    assert "Selected Google OAuth2 provider option" in entry["content"]
    assert "Test login succeeded." in entry["content"]
