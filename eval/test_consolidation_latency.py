"""Unit tests for Search Latency & Context Window Optimization (Phase 4)."""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from consolidation import compact_episodic_traces
from infra.migration_runner import run_migrations


@pytest.fixture
def benchmark_db(tmp_path: Path):
    db_path = tmp_path / 'test_latency.db'
    conn = sqlite3.connect(str(db_path))
    run_migrations(conn)

    # Insert 50 verbose episodic steps across 5 sessions (typical real DOM / tool trace length ~1KB each)
    cur = conn.cursor()
    for s_idx in range(5):
        sess_id = f"sess_{s_idx:04d}"
        for step in range(10):
            slug = f"step_{sess_id}_{step}"
            content = (
                f"[{sess_id}] Step {step} Observation: Checked parameter setting {step} for cluster node {s_idx}. "
                f"DOM Details: id=node_{s_idx}_row_{step}, class=status-healthy-active-table-view, "
                f"payload={{node_ip: '192.168.1.{s_idx}', uptime_seconds: {step * 3600}, cpu_percent: 14.2, memory_used_mb: 2048, "
                f"diagnostics: 'All self-tests passed without error codes, heartbeat active on port 9090', "
                f"rendered_elements: ['Header', 'Navbar', 'NodeList', 'DetailsPane', 'LogsModal', 'ActionButton']}}."
            )
            cur.execute(
                """INSERT INTO memories (id, source_file, content, category, importance, created_at, updated_at, observed_at)
                   VALUES (?, ?, ?, 'steps', 3, '2026-08-20T10:00:00Z', '2026-08-20T10:00:00Z', '2026-08-20T10:00:00Z')""",
                (f"mem_{slug}", f"steps/{slug}.md", content)
            )
    conn.commit()
    yield conn
    conn.close()


def test_consolidation_token_and_record_compression(benchmark_db):
    """Verify that consolidation reduces record count and prompt token footprint by >60%."""
    raw_count = benchmark_db.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
    assert raw_count == 50

    results = compact_episodic_traces(benchmark_db, min_steps=5, save_to_db=False)
    assert len(results) == 5  # 50 steps distilled into 5 high-density notes

    raw_chars = sum(len(r[0]) for r in benchmark_db.execute("SELECT content FROM memories").fetchall())
    consolidated_chars = sum(len(r["content"]) for r in results)

    # 50 verbose steps (~25KB) compacted into 5 structured summaries (~4KB)
    compression_ratio = 1.0 - (consolidated_chars / raw_chars)
    assert compression_ratio > 0.60
