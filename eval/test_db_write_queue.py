"""Concurrency stress tests for db_write_queue.py.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

sys.path.insert(0, os.path.expandvars("$HOME/.config/agentic-memory") or os.path.expanduser("~/.config/agentic-memory"))

from infra.db import open_db

from infra.db_write_queue import sqlite_write_queue  # noqa: E402


# 2026-07-08: bound the idle timeout so a forgotten/abandoned session
# cannot hold the RESERVED write lock on the DB for the legacy 3600s window.
# Set in module-scoped fixture to avoid leaking into other test modules.
_original_idle_s = os.environ.get("MEMORY_WRITE_QUEUE_IDLE_S")
_original_resp_timeout_s = os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S")


def _restore_env():
    if _original_idle_s is None:
        os.environ.pop("MEMORY_WRITE_QUEUE_IDLE_S", None)
    else:
        os.environ["MEMORY_WRITE_QUEUE_IDLE_S"] = _original_idle_s
    if _original_resp_timeout_s is None:
        os.environ.pop("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", None)
    else:
        os.environ["MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S"] = _original_resp_timeout_s


class TestDBWriteQueueConcurrency:
    def setup_method(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_concurrency.db"

        # Initialize schema
        with open_db(self.db_path, write=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS test_writes (id TEXT PRIMARY KEY, val INTEGER)"
            )

    def teardown_method(self):
        self.temp_dir.cleanup()

    def test_concurrent_writes_threads(self):
        num_threads = 50
        errors = []

        def worker(thread_idx):
            try:
                with open_db(self.db_path, write=True) as conn:
                    conn.execute(
                        "INSERT INTO test_writes (id, val) VALUES (?, ?)",
                        (f"thread_{thread_idx}", thread_idx)
                    )
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for future in as_completed(futures):
                future.result()

        # Assert no lock contention errors occurred
        assert len(errors) == 0, f"Encountered write errors: {errors}"

        # Verify that all 50 rows were written
        with open_db(self.db_path, write=False) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM test_writes").fetchone()
            assert rows is not None, "COUNT(*) returned None"
            rows = rows[0]
            assert rows == num_threads, f"Expected {num_threads} rows, but got {rows}"


class TestWriteQueueSessionTimeout:
    """Regression tests for the 2026-07-08 fix: a session that opens
    BEGIN IMMEDIATE (RESERVED lock) but never receives a ``close`` command
    must release the lock within a bounded idle window, not hold it for
    the legacy 3600s.
    """

    def setup_method(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_session.db"
        conn = __import__("sqlite3").connect(str(self.db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"
        )
        conn.commit()
        conn.close()

    def teardown_method(self):
        self.temp_dir.cleanup()

    def test_idle_session_releases_lock(self):
        """A session left open (no close sent) must release the lock within
        MEMORY_WRITE_QUEUE_IDLE_S seconds so competing writers can proceed."""
        sess = sqlite_write_queue.start_session(self.db_path)
        sess.execute("INSERT INTO t (v) VALUES (?)", ("held",))

        # A separate connection should be blocked only until the idle
        # timeout fires and the session force-rollbacks.
        result = {}

        def competing():
            import sqlite3

            c = sqlite3.connect(str(self.db_path), timeout=10.0)
            c.execute("PRAGMA busy_timeout=10000")
            t0 = time.time()
            try:
                c.execute("INSERT INTO t (v) VALUES (?)", ("competing",))
                c.commit()
                result["status"] = "ok"
                result["elapsed"] = time.time() - t0
            except sqlite3.OperationalError as e:
                result["status"] = f"fail: {e}"
            finally:
                c.close()

        th = threading.Thread(target=competing)
        th.start()
        th.join(timeout=15)

        # The competing write must succeed, and within ~idle_timeout + slack.
        assert result.get("status") == "ok", f"competing write failed: {result}"
        assert result.get("elapsed", 999) < 10.0, (
            f"lock held too long ({result.get('elapsed'):.1f}s) — idle timeout not releasing"
        )

    def test_abandoned_session_does_not_block_forever(self):
        """Even if the caller vanishes mid-session, a fresh caller's writes
        must eventually succeed (bounded by idle timeout)."""
        sess = sqlite_write_queue.start_session(self.db_path)
        sess.execute("INSERT INTO t (v) VALUES (?)", ("abandoned",))
        # Deliberately do NOT call sess.close() — simulate a dead caller.

        # Give the idle timeout time to fire, then verify a new write works.
        time.sleep(3.0)
        import sqlite3

        c = sqlite3.connect(str(self.db_path), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        try:
            c.execute("INSERT INTO t (v) VALUES (?)", ("after_abandon",))
            c.commit()
            ok = True
        except sqlite3.OperationalError:
            ok = False
        finally:
            c.close()
        assert ok, "write after abandoned session failed — lock not released"


@pytest.fixture(autouse=True, scope="module")
def _set_and_restore_write_queue_env():
    os.environ["MEMORY_WRITE_QUEUE_IDLE_S"] = "2"
    os.environ["MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S"] = "10"
    yield
    _restore_env()
