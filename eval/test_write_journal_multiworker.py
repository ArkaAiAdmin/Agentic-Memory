"""Tests for sharded dequeue claim — Step 1 of multi-writer materialization.

These tests exercise ``dequeue_pending_for_worker`` directly using
in-process threads.  The journal DB is a tmp_path fixture so there
is no interaction with the live memory store.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from infra.write_journal import (
    _clear_local_conns,
    dequeue_pending_for_worker,
    init_journal_db,
)


def _populate_journal(tmp_path: Path, n: int = 1000) -> Path:
    """Create a journal DB with *n* pending rows and return its path."""
    jp = tmp_path / "journal.db"
    init_journal_db(jp)

    def _insert() -> None:
        conn = sqlite3.connect(str(jp), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        for i in range(n):
            conn.execute(
                "INSERT INTO write_journal "
                "(note_id, agent_id, category, title_slug, content, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (f"note-{i}", "agent-t", "lessons", f"slug-{i}", f"content-{i}"),
            )
        conn.commit()
        conn.close()

    _insert()
    _clear_local_conns()
    return jp


class TestShardedDequeue:
    """In-process tests for dequeue_pending_for_worker."""

    def test_no_collisions_with_4_workers(self, tmp_path: Path) -> None:
        jp = _populate_journal(tmp_path, n=1000)

        results: dict[int, list[dict]] = {k: [] for k in range(4)}
        threads: list[threading.Thread] = []

        def _worker(wid: int) -> None:
            results[wid] = dequeue_pending_for_worker(jp, batch_size=250, worker_id=wid, n_workers=4)

        for k in range(4):
            t = threading.Thread(target=_worker, args=(k,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        all_ids: list[int] = []
        for wid in range(4):
            rows = results[wid]
            ids = [r["id"] for r in rows]
            assert ids == sorted(ids), f"worker {wid}: ids not monotonic"
            for r in rows:
                assert r["id"] % 4 == wid, f"worker {wid}: row {r['id']} wrong shard"
            all_ids.extend(ids)

        assert len(all_ids) == len(set(all_ids)), "ID collision detected"
        assert sorted(all_ids) == list(range(1, 1001)), "not all rows claimed"

    def test_no_collisions_with_8_workers(self, tmp_path: Path) -> None:
        jp = _populate_journal(tmp_path, n=10000)

        results: dict[int, list[dict]] = {k: [] for k in range(8)}
        threads: list[threading.Thread] = []

        def _worker(wid: int) -> None:
            results[wid] = dequeue_pending_for_worker(jp, batch_size=1250, worker_id=wid, n_workers=8)

        for k in range(8):
            t = threading.Thread(target=_worker, args=(k,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        all_ids: list[int] = []
        for wid in range(8):
            rows = results[wid]
            ids = [r["id"] for r in rows]
            assert ids == sorted(ids), f"worker {wid}: ids not monotonic"
            for r in rows:
                assert r["id"] % 8 == wid, f"worker {wid}: row {r['id']} wrong shard"
            all_ids.extend(ids)

        assert len(all_ids) == len(set(all_ids)), "ID collision detected"
        assert sorted(all_ids) == list(range(1, 10001)), "not all rows claimed"

    def test_backward_compat_dequeue_pending(self, tmp_path: Path) -> None:
        """dequeue_pending (no sharding args) still works."""
        jp = _populate_journal(tmp_path, n=50)

        from infra.write_journal import dequeue_pending

        rows = dequeue_pending(jp, batch_size=50)
        assert len(rows) == 50
        assert all(r["status"] == "processing" for r in rows)
        assert all(r.get("worker_id", 0) == 0 for r in rows)

    def test_crash_recovery_stuck_processing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jp = _populate_journal(tmp_path, n=10)

        worker_1_entries = dequeue_pending_for_worker(jp, batch_size=10, worker_id=1, n_workers=4)
        assert len(worker_1_entries) == 3, f"worker 1: expected 3 entries, got {len(worker_1_entries)}"
        reclaimed_ids = {e['id'] for e in worker_1_entries}

        from infra.write_journal import reset_stuck_processing

        # Wait until the claimed entries exceed the stuck threshold (2s).
        # We pass max_age_seconds=2 directly so we bypass the module-level
        # constant (which is cached at import time from the env var).
        conn_check = sqlite3.connect(str(jp), timeout=5)
        row = conn_check.execute(
            "SELECT MIN(started_at) FROM write_journal WHERE status='processing'"
        ).fetchone()
        conn_check.close()
        import datetime
        if row and row[0]:
            started_dt = datetime.datetime.fromisoformat(row[0])
            now_dt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            already_stuck = max((now_dt - started_dt).total_seconds(), 0)
            if already_stuck < 2.0:
                import time as _time
                _time.sleep(2.0 - already_stuck + 0.5)

        n_reset = reset_stuck_processing(jp, max_age_seconds=2)
        assert n_reset == 3, f"Expected 3 stuck entries reset, got {n_reset}"

        reclaimed = dequeue_pending_for_worker(jp, batch_size=10, worker_id=1, n_workers=4)
        reclaimed_ids_after = {r['id'] for r in reclaimed}
        assert reclaimed_ids_after == reclaimed_ids, (
            f"Reclaimed ids {reclaimed_ids_after} != original {reclaimed_ids}"
        )
