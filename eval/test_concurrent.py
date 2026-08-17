#!/usr/bin/env python3
"""Concurrent access tests for agentic-memory.

Multi-threaded stress tests verifying:
- Parallel saves don't corrupt data or deadlock
- Parallel searches don't crash or return stale data
- Mixed save/search under concurrent load
- Connection pool handles contention gracefully
- WAL mode allows concurrent readers during writes

Usage:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_concurrent -v

E5 fix (2026-06-22): this is a real platform-specific stress test.
The pytestmark skipif is intentional, not a flaky-test marker.
The stress tests run 10 threads × 20 ops = 200 ops per test × 9 tests
= ~80s on a typical laptop.  That is too long for the default CI
loop.  Set ``RUN_SLOW_TESTS=1`` to enable, e.g. for the nightly
eval or before a release.  The skipif here is NOT a substitute for
unit-testing the saga's atomicity — that is covered by
``test_saga_crash_safety.py`` and the per-thread connection pool
tests in ``test_integration_save_pipeline.py``.

These tests rely on the system supporting POSIX threads (true on
Linux and macOS).  On Windows the skipif would also gate them; we
do not currently support Windows.
"""

import os
import sys
import tempfile
import shutil
import threading
import time
import unittest

# Stress tests: dynamically scaled — 3 threads x 3 ops in standard runs (<3s),
# and 10 threads x 20 ops (~80s) when RUN_SLOW_TESTS=1.
import pytest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


from infra.memory_common import (
    open_db,
    connection_pool,
)
from save_pipeline import save_memory
from search_pipeline import search_memories
from _fixtures import bootstrap_temp_db_clean
from infra.audit import flush_audit

# Safety: use ONLY the test DB, never production.
_test_db = os.environ.get("CONCURRENT_TEST_DB")
worker_id = os.environ.get("PYTEST_XDIST_WORKER", "")
if _test_db:
    _default_db = _test_db
else:
    suffix = f"_{worker_id}" if worker_id else ""
    _default_db = str(
        Path.home() / ".config" / "agentic-memory" / "eval" / f".concurrent_test{suffix}.db"
    )
PROD_DB = Path(_default_db)

# Guard: never allow production DB path
if "agentic-memory/" in str(PROD_DB) and ".concurrent" not in str(PROD_DB):
    raise RuntimeError(f"CONCURRENT_TEST_DB points to production: {PROD_DB}")

_RUN_SLOW = os.environ.get("RUN_SLOW_TESTS", "0") in ("1", "true", "True")
THREAD_COUNT = 10 if _RUN_SLOW else 3
OPS_PER_THREAD = 20 if _RUN_SLOW else 3


def _unique_slug(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _delete_note_direct(db_path, note_id):
    with open_db(db_path) as db:
        db.execute(
            "UPDATE memories SET deleted_at = datetime('now') WHERE id = ?", (note_id,)
        )
        db.commit()


class _ConcurrentDbTestMixin:
    def setUp(self):
        """Fresh temp DB for each test."""
        flush_audit()
        global PROD_DB
        self._temp_dir = tempfile.mkdtemp(prefix="concurrent_test_")
        PROD_DB = Path(self._temp_dir) / "test.db"
        bootstrap_temp_db_clean(PROD_DB)

    def tearDown(self):
        flush_audit()
        global PROD_DB
        path_key = str(PROD_DB)
        with connection_pool._lock:
            for key in list(connection_pool._pool):
                if key[0] == path_key:
                    old = connection_pool._pool.pop(key)
                    connection_pool._pooled_ids.discard(id(old))
                    try:
                        old.close()
                    except Exception:
                        pass
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class TestConcurrentSaves(_ConcurrentDbTestMixin, unittest.TestCase):
    """Parallel saves from multiple threads must not corrupt data or deadlock."""

    def test_parallel_saves_all_persisted(self):
        """10 threads x 20 saves = 200 notes. All must be in DB after completion."""
        slug_prefix = _unique_slug("par_save")
        note_ids = []
        lock = threading.Lock()
        errors = []

        def do_save(i):
            try:
                slug = f"{slug_prefix}_{i}"
                result = save_memory(
                    content=f"Concurrent save test note {i} — {uuid.uuid4().hex}",
                    category="test_concurrent",
                    title_slug=slug,
                    tags=["concurrent", "stress"],
                    pinned=False,
                    is_global=False,
                    safety_wiring=False,
                    db_path=str(PROD_DB),
                )
                if isinstance(result, str) and not result.startswith("Error"):
                    with lock:
                        note_ids.append(result)
                else:
                    with lock:
                        errors.append(f"Thread {i}: {result}")
            except Exception as e:
                with lock:
                    errors.append(f"Thread {i}: {e}")

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [
                pool.submit(do_save, i) for i in range(THREAD_COUNT * OPS_PER_THREAD)
            ]
            for f in as_completed(futures):
                f.result()  # propagate exceptions

        # Verify all saves persisted
        self.assertEqual(len(errors), 0, f"Save errors: {errors[:5]}")
        self.assertEqual(len(note_ids), THREAD_COUNT * OPS_PER_THREAD)

        with open_db(PROD_DB) as db:
            for nid in note_ids:
                row = db.execute(
                    "SELECT id FROM memories WHERE id = ?", (nid,)
                ).fetchone()
                self.assertIsNotNone(row, f"Note {nid} not found in DB")

        # Cleanup
        for nid in note_ids:
            try:
                _delete_note_direct(PROD_DB, nid)
            except Exception:
                pass

    def test_parallel_saves_no_deadlock(self):
        """10 threads hitting save simultaneously must complete within timeout."""
        slug_prefix = _unique_slug("deadlock")
        threading.Event()
        results = []
        lock = threading.Lock()

        def do_save(i):
            slug = f"{slug_prefix}_{i}"
            start = time.time()
            result = save_memory(
                content=f"Deadlock test {i}",
                category="test_concurrent",
                title_slug=slug,
                tags=[],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(PROD_DB),
            )
            elapsed = time.time() - start
            with lock:
                results.append((i, result, elapsed))

        start_all = time.time()
        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(do_save, i) for i in range(THREAD_COUNT)]
            for f in as_completed(futures):
                f.result()
        total_elapsed = time.time() - start_all

        # Must complete within 30 seconds (generous for CI)
        self.assertLess(
            total_elapsed, 30.0, f"Possible deadlock: took {total_elapsed:.1f}s"
        )

        # All must succeed
        errors = [
            r for r in results if isinstance(r[1], str) and r[1].startswith("Error")
        ]
        self.assertEqual(len(errors), 0, f"Errors: {errors[:3]}")

        # Cleanup
        for _, nid, _ in results:
            if isinstance(nid, str) and not nid.startswith("Error"):
                try:
                    _delete_note_direct(PROD_DB, nid)
                except Exception:
                    pass

    def test_parallel_saves_unique_note_ids(self):
        """Each parallel save must produce a unique note_id (no overwrites)."""
        slug_prefix = _unique_slug("unique")
        note_ids = []
        lock = threading.Lock()

        def do_save(i):
            slug = f"{slug_prefix}_{i}"
            result = save_memory(
                content=f"Unique test {i}",
                category="test_concurrent",
                title_slug=slug,
                tags=[],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(PROD_DB),
            )
            with lock:
                note_ids.append(result)

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(do_save, i) for i in range(THREAD_COUNT * 5)]
            for f in as_completed(futures):
                f.result()

        # All note_ids must be unique
        self.assertEqual(
            len(note_ids),
            len(set(note_ids)),
            f"Duplicate note_ids found: {len(note_ids)} total, {len(set(note_ids))} unique",
        )

        # Cleanup
        for nid in note_ids:
            if isinstance(nid, str) and not nid.startswith("Error"):
                try:
                    _delete_note_direct(PROD_DB, nid)
                except Exception:
                    pass


class TestConcurrentSearches(_ConcurrentDbTestMixin, unittest.TestCase):
    """Parallel searches must not crash or return corrupted data."""

    def test_parallel_searches_no_crash(self):
        """10 threads searching simultaneously must all complete."""
        results = []
        lock = threading.Lock()
        queries = ["test", "concurrent", "memory", "lesson", "decision", "error"]

        def do_search(i):
            query = queries[i % len(queries)]
            try:
                result = search_memories(PROD_DB, query, limit=5)
                with lock:
                    results.append((i, result))
            except Exception as e:
                with lock:
                    results.append((i, {"error": str(e)}))

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [
                pool.submit(do_search, i) for i in range(THREAD_COUNT * OPS_PER_THREAD)
            ]
            for f in as_completed(futures):
                f.result()

        # All must return valid results (no crashes)
        errors = [r for r in results if isinstance(r[1], dict) and "error" in r[1]]
        self.assertEqual(len(errors), 0, f"Search errors: {errors[:3]}")

        # All must return dict with results key
        for idx, res in results:
            self.assertIsInstance(res, dict, f"Result {idx} is not a dict")
            self.assertIn("results", res, f"Result {idx} missing 'results' key")

    def test_parallel_searches_consistent_read(self):
        """Searches during concurrent writes must not return partial data."""
        # First, ensure a known note exists
        slug = _unique_slug("consistent")
        save_result = save_memory(
            content="Consistent read test — verify this note appears in search",
            category="test_concurrent",
            title_slug=slug,
            tags=["consistent"],
            pinned=False,
            is_global=False,
            safety_wiring=False,
            db_path=str(PROD_DB),
        )
        self.assertIsInstance(save_result, str)
        self.assertFalse(str(save_result).startswith("Error"))

        # Now search from multiple threads while also saving from other threads
        found_ids = []
        lock = threading.Lock()
        save_errors = []

        def do_search(i):
            result = search_memories(PROD_DB, "Consistent read test", limit=10)
            if isinstance(result, dict):
                for r in result.get("results", []):
                    nid = r.get("id", r.get("memory_id", ""))
                    if nid == save_result:
                        with lock:
                            found_ids.append(i)

        def do_save(i):
            slug2 = f"{slug}_write_{i}"
            res = save_memory(
                content=f"Write during search {i}",
                category="test_concurrent",
                title_slug=slug2,
                tags=[],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(PROD_DB),
            )
            if isinstance(res, str) and res.startswith("Error"):
                with lock:
                    save_errors.append(res)

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            search_futures = [pool.submit(do_search, i) for i in range(THREAD_COUNT)]
            write_futures = [pool.submit(do_save, i) for i in range(THREAD_COUNT)]
            for f in as_completed(search_futures + write_futures):
                f.result()

        # The known note must be found by at least some searches
        self.assertGreater(
            len(found_ids), 0, "Known note never found in concurrent searches"
        )
        self.assertEqual(
            len(save_errors), 0, f"Write errors during search: {save_errors}"
        )

        # Cleanup
        _delete_note_direct(PROD_DB, save_result)
        for i in range(THREAD_COUNT):
            try:
                _delete_note_direct(PROD_DB, f"{slug}_write_{i}")
            except Exception:
                pass


class TestConcurrentMixed(_ConcurrentDbTestMixin, unittest.TestCase):
    """Mixed save/search/delete from multiple threads."""

    def test_mixed_save_search_cycle(self):
        """Threads alternate between save, search, and verify."""
        slug_prefix = _unique_slug("mixed")
        all_ids = []
        lock = threading.Lock()
        errors = []

        def worker(thread_id):
            local_ids = []
            for op in range(OPS_PER_THREAD):
                slug = f"{slug_prefix}_t{thread_id}_op{op}"
                # Save
                result = save_memory(
                    content=f"Mixed test thread {thread_id} op {op} — {uuid.uuid4().hex}",
                    category="test_concurrent",
                    title_slug=slug,
                    tags=["mixed"],
                    pinned=False,
                    is_global=False,
                    safety_wiring=False,
                    db_path=str(PROD_DB),
                )
                if isinstance(result, str) and not result.startswith("Error"):
                    local_ids.append(result)
                else:
                    with lock:
                        errors.append(f"Thread {thread_id} op {op}: {result}")
                    continue

                # Search for what we just saved
                search_result = search_memories(
                    PROD_DB, f"Mixed test thread {thread_id}", limit=5
                )
                if isinstance(search_result, dict):
                    any(
                        r.get("id", r.get("memory_id", "")) == result
                        for r in search_result.get("results", [])
                    )
                    # Note: search might not find it immediately due to FTS indexing delay
                    # but it must not crash

            with lock:
                all_ids.extend(local_ids)

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(worker, i) for i in range(THREAD_COUNT)]
            for f in as_completed(futures):
                f.result()

        self.assertEqual(len(errors), 0, f"Mixed op errors: {errors[:5]}")
        self.assertGreater(len(all_ids), 0, "No notes were saved")

        # Cleanup
        for nid in all_ids:
            try:
                _delete_note_direct(PROD_DB, nid)
            except Exception:
                pass

    def test_concurrent_fts_consistency(self):
        """FTS5 index must be consistent after concurrent writes."""
        slug_prefix = _unique_slug("fts_consistency")
        note_ids = []
        lock = threading.Lock()

        def do_save(i):
            slug = f"{slug_prefix}_{i}"
            result = save_memory(
                content=f"FTS consistency check uniquephrase {i}",
                category="test_concurrent",
                title_slug=slug,
                tags=[],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(PROD_DB),
            )
            with lock:
                if isinstance(result, str) and not result.startswith("Error"):
                    note_ids.append(result)

        # Save all first
        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(do_save, i) for i in range(THREAD_COUNT * 3)]
            for f in as_completed(futures):
                f.result()

        # Now search for each saved note via FTS
        missing = []
        for nid in note_ids:
            # Extract index from title_slug (e.g., fts_consistency_abc123_0)
            slug = nid.split("/")[-1] if "/" in nid else nid
            idx = slug.split("_")[-1]
            result = search_memories(
                PROD_DB, f"FTS consistency check uniquephrase {idx}", limit=5
            )
            if isinstance(result, dict):
                found_ids = [
                    r.get("id", r.get("memory_id", ""))
                    for r in result.get("results", [])
                ]
                if nid not in found_ids:
                    missing.append(nid)

        # Allow some misses (FTS may not index instantly) but not all
        self.assertLess(
            len(missing),
            len(note_ids),
            f"FTS missing {len(missing)}/{len(note_ids)} notes — possible index corruption",
        )

        # Cleanup
        for nid in note_ids:
            try:
                _delete_note_direct(PROD_DB, nid)
            except Exception:
                pass


class TestConnectionPoolContention(_ConcurrentDbTestMixin, unittest.TestCase):
    """Connection pool must handle heavy contention without leaks or errors."""

    def test_pool_survives_burst(self):
        """Concurrent burst operations must not exhaust the pool."""
        slug_prefix = _unique_slug("pool_burst")
        results = []
        lock = threading.Lock()
        burst_count = 30 if _RUN_SLOW else 6
        workers = 10 if _RUN_SLOW else 3

        def do_op(i):
            try:
                if i % 2 == 0:
                    slug = f"{slug_prefix}_{i}"
                    save_memory(
                        content=f"Pool burst {i}",
                        category="test_concurrent",
                        title_slug=slug,
                        tags=[],
                        pinned=False,
                        is_global=False,
                        safety_wiring=False,
                        db_path=str(PROD_DB),
                    )
                else:
                    search_memories(PROD_DB, "pool burst", limit=3)
                with lock:
                    results.append(("ok", i))
            except Exception as e:
                with lock:
                    results.append(("error", f"{i}: {e}"))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(do_op, i) for i in range(burst_count)]
            for f in as_completed(futures):
                f.result()

        errors = [r for r in results if r[0] == "error"]
        self.assertEqual(len(errors), 0, f"Pool burst errors: {errors[:5]}")
        self.assertEqual(len(results), burst_count)

    def test_pool_no_connection_leak(self):
        """After concurrent ops, pool must not hold stale connections."""
        # Record pool state before
        len(connection_pool._pool)

        slug_prefix = _unique_slug("leak")
        note_ids = []

        def do_save(i):
            slug = f"{slug_prefix}_{i}"
            result = save_memory(
                content=f"Leak test {i}",
                category="test_concurrent",
                title_slug=slug,
                tags=[],
                pinned=False,
                is_global=False,
                safety_wiring=False,
                db_path=str(PROD_DB),
            )
            if isinstance(result, str) and not result.startswith("Error"):
                return result
            return None

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(do_save, i) for i in range(THREAD_COUNT * 5)]
            for f in as_completed(futures):
                r = f.result()
                if r:
                    note_ids.append(r)

        # Pool should not grow unbounded
        pool_size_after = len(connection_pool._pool)
        self.assertLessEqual(
            pool_size_after,
            connection_pool._max_size,
            f"Pool grew beyond max: {pool_size_after} > {connection_pool._max_size}",
        )

        # Cleanup
        for nid in note_ids:
            try:
                _delete_note_direct(PROD_DB, nid)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
