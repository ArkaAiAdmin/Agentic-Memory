"""Tests for the brief idempotent guard — Step 2 of multi-writer materialization.

Two tests:
1. Two threads materializing the same entry simultaneously — only one row
   in memories, both threads return the note_id.
2. Idempotent pre-existence guard — calling _materialize_journal_entry
   twice for the same entry returns silently the second time.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from save.pipeline import materialize_journal_entry  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA foreign_keys=ON;")
    db.execute("PRAGMA busy_timeout = 5000;")
    return db


def _init_schema(db: sqlite3.Connection) -> None:
    from infra.memory_common import run_db_migrations, _migrate_kg_tables  # noqa: F401
    from fact import ensure_facts_schema  # noqa: F401
    from adaptive_retention import ensure_adaptive_schema  # noqa: F401
    run_db_migrations(db)
    _migrate_kg_tables(db)
    ensure_facts_schema(db)
    ensure_adaptive_schema(db)
    db.commit()


def _fresh_db() -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="idemtest_"))
    db_path = tmpdir / "memory.db"
    db = _make_db(db_path)
    _init_schema(db)
    db.close()
    return db_path


def _make_minimal_journal_entry(journal_path: Path) -> dict:
    from infra.write_journal import init_journal_db, enqueue_write, SaveRequest, _clear_local_conns

    init_journal_db(journal_path)
    req = SaveRequest(
        content="idempotent guard test content",
        category="lessons",
        title_slug="idempotent-guard-test",
        tags=["test"],
        pinned=False,
        is_global=False,
        importance=3,
        context="generic",
        defer_expensive=True,
        tenant_id="default",
        epistemic_source="agent",
        belief_status="active",
        asserting_agent_id="",
        fact_type="observation",
    )
    note_id = enqueue_write(journal_path, req, agent_id="test-agent")
    conn = sqlite3.connect(str(journal_path), timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM write_journal WHERE note_id=?", (note_id,)
    ).fetchone()
    result = dict(row)
    conn.close()
    _clear_local_conns()
    return result


class TestIdempotentMaterialize:
    """Step 2: two reconcilers materializing the same entry."""

    def test_concurrent_double_materialize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads materialise the same entry: one row in memories."""
        monkeypatch.setenv("MEMORY_DB_FLOCK", "0")

        target_dir = tmp_path / "mem"
        target_dir.mkdir(parents=True, exist_ok=True)
        mem_db_path = target_dir / "memory.db"
        db = _make_db(mem_db_path)
        _init_schema(db)
        db.close()

        journal_path = tmp_path / "journal.db"
        entry = _make_minimal_journal_entry(journal_path)

        results: list[tuple[str, BaseException | None]] = []
        lock = threading.Lock()

        def _mat() -> None:
            try:
                note_id_out = materialize_journal_entry(entry, target_dir)
                with lock:
                    results.append((str(note_id_out), None))
            except BaseException as exc:
                with lock:
                    results.append((entry["note_id"], exc))

        t1 = threading.Thread(target=_mat)
        t2 = threading.Thread(target=_mat)
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        assert len(results) == 2, f"Expected 2 results, got {results}"
        note_ids = [r[0] for r in results]
        errors = [r[1] for r in results]
        assert all(e is None for e in errors), f"Errors: {errors}"
        assert note_ids[0] == note_ids[1] == entry["note_id"]

        conn = sqlite3.connect(str(mem_db_path))
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (entry["note_id"],)
        ).fetchone()[0]
        conn.close()
        assert n_rows == 1, f"Expected 1 row in memories, got {n_rows}"

    def test_idempotent_guard_skips_duplicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second call to materialize for the same entry returns early."""
        monkeypatch.setenv("MEMORY_DB_FLOCK", "0")

        target_dir = tmp_path / "mem2"
        target_dir.mkdir(parents=True, exist_ok=True)
        mem_db_path = target_dir / "memory.db"
        db = _make_db(mem_db_path)
        _init_schema(db)
        db.close()

        journal_path = tmp_path / "journal2.db"
        entry = _make_minimal_journal_entry(journal_path)

        note_id_1st = materialize_journal_entry(entry, target_dir)
        assert note_id_1st == entry["note_id"]

        note_id_2nd = materialize_journal_entry(entry, target_dir)
        assert note_id_2nd == entry["note_id"]

        conn = sqlite3.connect(str(mem_db_path))
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (entry["note_id"],)
        ).fetchone()[0]
        conn.close()
        assert n_rows == 1
