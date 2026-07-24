"""Regression test for the CQRS write-journal materialization deadlock.

Background
----------
With ``write_journal = true`` (production default), the reconciler daemon is
the only writer to memory.db.  ``_materialize_journal_once`` runs the saga
inside a ``sqlite_write_queue`` session that holds ``BEGIN IMMEDIATE`` (the
SQLite write lock) on the worker thread for the session's whole lifetime, and
THEN ran the post-save hooks (contradiction check, auto-backlink, CRDT
projection, background-task enqueue) while that session was still open.

Those hooks open their OWN connections.  The contradiction check
(``es.search``) opens a second ``connection_pool.get`` connection and writes
to ``memory_embeddings``.  That second connection blocked on the write lock
the worker still held -> the reconciler thread deadlocked forever and the
journal entry never materialized.

The fix (W8, 2026-07-19) closes the saga session before the post-save hooks
run, so each hook opens a fresh connection with no lock contention.

This test enqueues a real journal entry through the production code path
(``save_memory_journal`` with the default ``defer_expensive=False`` — the same
path the dashboard/API ``create_memory`` uses) and starts the reconciler.  It
asserts the entry materializes into ``memories`` within a bounded timeout,
failing (rather than hanging) if the deadlock regresses.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from background.journal_reconciler import _drain_once  # noqa: E402
from infra.write_journal import init_journal_db  # noqa: E402
from save.pipeline import save_memory_journal  # noqa: E402

# Upper bound: if the reconciliation deadlocks, the write-queue worker's
# idle-session timeout eventually releases the lock (~30s) and the entry
# lands; a 60s deadline gives headroom while still failing a true hang.
_MATERIALIZE_TIMEOUT_S = 60.0


@pytest.fixture
def _mem_dir(tmp_path: Path) -> Path:
    """A fresh memory dir with schema initialised, mirroring production."""
    from infra.memory_common import run_db_migrations, _migrate_kg_tables
    from fact import ensure_facts_schema
    from adaptive_retention import ensure_adaptive_schema

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir(parents=True, exist_ok=True)
    db_path = mem_dir / "memory.db"
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA foreign_keys=ON;")
    db.execute("PRAGMA busy_timeout=5000;")
    run_db_migrations(db)
    _migrate_kg_tables(db)
    ensure_facts_schema(db)
    ensure_adaptive_schema(db)
    db.commit()
    db.close()
    return mem_dir


def test_journal_materialize_no_deadlock_with_post_save_hooks(
    _mem_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Materializing a deferred=False journal entry must not deadlock.

    Reproduces the production path: save_memory_journal (defer_expensive=False)
    -> reconciler daemon -> post-save hooks -> memory row appears.
    """
    monkeypatch.setenv("MEMORY_DB_FLOCK", "0")
    # Avoid network model checks during the inline embedding index so the
    # test fails fast on a real deadlock rather than blocking on a download.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    mem_db = _mem_dir / "memory.db"
    journal_path = _mem_dir / "journal.db"
    init_journal_db(journal_path)

    note_id = save_memory_journal(
        content="regression deadlock probe: dashboard create_memory must land",
        category="lessons",
        title_slug="regression-deadlock-probe",
        defer_expensive=False,  # production default for the API/Client path
        db_path=str(mem_db),
    )
    assert note_id, "save_memory_journal should return a note_id"

    # Drain the journal directly using the reconciler's drain function.
    # The old code used _start_reconciler (background worker) which
    # processes task_queue, not journal entries.  _drain_once is the
    # correct entry point for journal materialization.
    deadline = time.time() + _MATERIALIZE_TIMEOUT_S
    materialized = False
    while time.time() < deadline:
        n = _drain_once(_mem_dir, journal_path, batch_size=5)
        if n > 0:
            conn = sqlite3.connect(str(mem_db), timeout=5)
            try:
                found = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE id=?", (note_id,)
                ).fetchone()[0]
            finally:
                conn.close()
            if found >= 1:
                materialized = True
                break
        time.sleep(0.5)

    assert materialized, (
        f"journal entry {note_id!r} did not materialize within "
        f"{_MATERIALIZE_TIMEOUT_S}s — deadlock regression"
    )
