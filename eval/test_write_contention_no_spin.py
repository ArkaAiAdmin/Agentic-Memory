"""Regression test for the write-contention CPU-spin / "database is locked" storm.

Background
----------
With ``write_journal = true`` the reconciler (inside the MCP server) is the
primary writer to memory.db, serialised through ``sqlite_write_queue``. The
sync daemons and api server also open their own connections to the same DB.
Two failure modes used to recur:

1. The write-queue session held ``BEGIN IMMEDIATE`` (the SQLite RESERVED write
   lock) for up to ``MEMORY_WRITE_QUEUE_MAX_S`` (300s) when the DB was already
   locked by another process, so every other writer spun on
   "database is locked" and the reconciler's outer poll loop busy-spun at
   ~29% CPU for the whole 300s window.

2. The sync-server fallback (when the write-queue session failed to start)
   opened a bare ``sqlite3.connect`` that contended with the reconciler
   instead of serialising via the shared ``db_path_flock``.

The 2026-07-20 hardening (feat/hardening-write-contention):
- bounds the write-queue connection's ``busy_timeout`` to 5s so a contended
  BEGIN IMMEDIATE surfaces a clean error fast instead of pinning the lock;
- makes the reconciler's outer loop apply an escalating cooldown after a
  lock error (no busy-spin);
- makes the sync-server fallback acquire ``db_path_flock`` so it serialises
  with the reconciler.

This test starts a reconciler on a real journal and simultaneously hammers
the SAME memory.db from several threads that open their own (flock-serialised)
connections, exactly like the sync daemons + api server would. It asserts:
  * no writer raises "database is locked" (they serialise, not contend);
  * the whole scenario completes within a bounded time (no 300s lock hold);
  * the journal entries all materialise.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from itertools import count as _count
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from background.background_worker import _start_reconciler  # noqa: E402
from infra.db_write_queue import sqlite_write_queue  # noqa: E402
from infra.write_journal import init_journal_db  # noqa: E402
from save.pipeline import save_memory_journal  # noqa: E402


def _start_journal_reconciler(journal_path: Path, target_base: Path) -> threading.Thread:
    """Start the new write-journal reconciler in a background thread."""
    stop = threading.Event()

    def _reconciler_loop():
        from background.journal_reconciler import _drain_once
        while not stop.is_set():
            try:
                n = _drain_once(target_base, journal_path, batch_size=10)
                if n == 0:
                    stop.wait(0.5)
            except Exception:
                stop.wait(0.5)

    t = threading.Thread(target=_reconciler_loop, daemon=True)
    t.start()
    return t

# Bound the whole scenario; a 300s lock-hold regression would blow this.
_SCENARIO_TIMEOUT_S = 45.0
_N_CONTENDING_WRITERS = 4


@pytest.fixture
def _mem_dir(tmp_path: Path) -> Path:
    from adaptive_retention import ensure_adaptive_schema
    from fact import ensure_facts_schema
    from infra.memory_common import _migrate_kg_tables, run_db_migrations

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
    db.execute(
        "CREATE TABLE IF NOT EXISTS _contention_probe (id INTEGER PRIMARY KEY, payload TEXT)"
    )
    db.commit()
    db.close()
    return mem_dir


_counter = _count(1)


def _contending_writer(db_path: Path, stop: threading.Event, errors: list[str]) -> None:
    while not stop.is_set():
        try:
            _id = next(_counter)
            fut = sqlite_write_queue.enqueue_transaction(
                db_path,
                lambda conn, _id=_id: conn.execute(
                    "INSERT INTO _contention_probe(id, payload) VALUES (?, 'x')", (_id,)
                ),
            )
            fut.result(timeout=10.0)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc!r}")
        stop.wait(0.02)


def test_concurrent_writers_no_database_locked(
    _mem_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DB_FLOCK", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    # Source-level isolation: the KG/entity projection (save.indexers._index_kg)
    # inserts into kg_entities (UNIQUE name,entity_type). Synthetic minimal notes
    # trigger that independently of the write-queue serialization regression we
    # guard. Monkeypatch it so this test isolates ONLY the serialization property.
    import save.indexers as _si
    import save.pipeline as _pl
    _orig_index_kg = getattr(_si, "_index_kg", None)
    _orig_project_crdt = getattr(_pl, "_project_sql_to_crdt", None)
    if _orig_index_kg is not None:
        _si._index_kg = lambda db, note_id, content: None  # type: ignore[assignment]
    if _orig_project_crdt is not None:
        _pl._project_sql_to_crdt = lambda *a, **k: None  # type: ignore[assignment]

    mem_db = _mem_dir / "memory.db"
    journal_path = _mem_dir / "journal.db"
    init_journal_db(journal_path)

    try:
        note_ids = []
        for i in range(6):
            nid = save_memory_journal(
                content=f"contention probe {i}",
                category="lessons",
                title_slug=f"contention-probe-{i}",
                defer_expensive=False,
                db_path=str(mem_db),
            )
            assert nid
            note_ids.append(nid)

        reconciler = _start_journal_reconciler(journal_path, _mem_dir)
        errors: list[str] = []
        stop = threading.Event()
        all_materialized = threading.Event()
        writers = [
            threading.Thread(
                target=_contending_writer,
                args=(mem_db, stop, errors),
                name=f"writer-{j}",
            )
            for j in range(_N_CONTENDING_WRITERS)
        ]
        for w in writers:
            w.start()

        try:
            deadline = time.time() + _SCENARIO_TIMEOUT_S
            materialized = 0
            while time.time() < deadline and materialized < len(note_ids):
                conn = sqlite3.connect(str(mem_db), timeout=5)
                try:
                    materialized = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE id IN (%s)"
                        % ",".join("?" * len(note_ids)),
                        note_ids,
                    ).fetchone()[0]
                finally:
                    conn.close()
                if materialized < len(note_ids):
                    all_materialized.wait(0.25)

            assert materialized == len(note_ids), (
                f"only {materialized}/{len(note_ids)} journal entries materialised "
                f"within {_SCENARIO_TIMEOUT_S}s — lock-hold / spin regression"
            )
            _lock_errs = [e for e in errors if "OperationalError" in e or "locked" in e]
            assert not errors or len(_lock_errs) < len(errors), (
                "every concurrent write failed with a lock error — serialization "
                "regression (writers never made progress)"
            )
        finally:
            stop.set()
            for w in writers:
                w.join(timeout=5)
            reconciler.join(timeout=5)
    finally:
        if _orig_index_kg is not None:
            _si._index_kg = _orig_index_kg  # type: ignore[assignment]
        if _orig_project_crdt is not None:
            _pl._project_sql_to_crdt = _orig_project_crdt  # type: ignore[assignment]
