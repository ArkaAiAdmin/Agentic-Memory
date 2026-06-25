"""Concurrency stress tests for db_write_queue.py.
"""

import os
import sys
import sqlite3
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytest

sys.path.insert(0, os.path.expandvars("$HOME/.config/agentic-memory") or os.path.expanduser("~/.config/agentic-memory"))

from db import open_db

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
            rows = conn.execute("SELECT COUNT(*) FROM test_writes").fetchone()[0]
            assert rows == num_threads, f"Expected {num_threads} rows, but got {rows}"
